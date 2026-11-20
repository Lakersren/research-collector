"""Existing MySQL tables only. No DDL or infrastructure provisioning."""
import json
import os
import ssl
from datetime import datetime, timezone

SCHEMA = {
    "source": "id name channel trust note first_ts last_ts n_docs".split(),
    "doc": "id source_id title authors published_at url content_state content ingested_at".split(),
    "doc_image": "doc_id position file_key status width height".split(),
    "collection_run": "id source_id started_at finished_at n_discovered n_fetched n_failed status error".split(),
}

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def connect():
    import pymysql
    required = ["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE"]
    missing = [k for k in required if k not in os.environ or (k != "MYSQL_PASSWORD" and not os.environ[k])]
    if missing:
        raise ValueError("缺少配置: " + ", ".join(missing))
    kw = dict(host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
              user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
              database=os.environ["MYSQL_DATABASE"], charset="utf8mb4", autocommit=False,
              connect_timeout=10, read_timeout=60, write_timeout=60)
    mode = os.getenv("MYSQL_SSL_MODE", "VERIFY_IDENTITY" if os.getenv("MYSQL_SSL_CA") else "DISABLED").upper()
    if mode not in ("DISABLED", "REQUIRED", "VERIFY_IDENTITY"):
        raise ValueError("MYSQL_SSL_MODE 必须为 DISABLED、REQUIRED 或 VERIFY_IDENTITY")
    if mode == "VERIFY_IDENTITY":
        kw["ssl"] = ssl.create_default_context(cafile=os.getenv("MYSQL_SSL_CA"))
    elif mode == "REQUIRED":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kw["ssl"] = context
    connection = pymysql.connect(**kw)
    if mode != "DISABLED":
        try:
            with connection.cursor() as c:
                c.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                cipher = c.fetchone()
                if not cipher or not cipher[1]:
                    raise ValueError("MySQL 未建立要求的 TLS 连接")
        except Exception:
            connection.close()
            raise
    return connection

class Repository:
    def __init__(self, connection, commit_each=True):
        self.conn = connection
        self.commit_each = commit_each

    def _commit(self):
        if self.commit_each: self.conn.commit()

    def _rollback(self):
        if self.commit_each: self.conn.rollback()

    def validate(self):
        # Read-only inspection. Missing columns are reported to IT, never created.
        with self.conn.cursor() as c:
            for table, expected in SCHEMA.items():
                c.execute(f"SHOW COLUMNS FROM `{table}`")
                actual = {row[0] for row in c.fetchall()}
                missing = set(expected) - actual
                if missing:
                    raise ValueError(f"{table} 缺少字段: {', '.join(sorted(missing))}")
        self._commit()

    def start(self, source, run_id=None, started_at=None):
        try:
            with self.conn.cursor() as c:
                c.execute("""INSERT INTO `source` (`id`,`name`,`channel`,`trust`,`note`,`n_docs`)
                    VALUES (%s,%s,'web',3,%s,0) ON DUPLICATE KEY UPDATE `name`=VALUES(`name`)""",
                          (source['key'], source['name'], source.get('note')))
                if run_id is None:
                    c.execute("""INSERT INTO `collection_run`
                        (`source_id`,`started_at`,`n_discovered`,`n_fetched`,`n_failed`,`status`)
                        VALUES (%s,%s,0,0,0,'running')""", (source['key'], started_at or utcnow()))
                    run_id = c.lastrowid
                else:
                    c.execute("""INSERT INTO `collection_run`
                        (`id`,`source_id`,`started_at`,`n_discovered`,`n_fetched`,`n_failed`,`status`)
                        VALUES (%s,%s,%s,0,0,0,'running')""", (run_id,source['key'],started_at or utcnow()))
            self._commit()
            return run_id
        except Exception:
            self._rollback()
            raise

    def save_article(self, doc, images):
        cols = SCHEMA['doc']
        row = {**doc, 'authors': json.dumps(doc['authors'], ensure_ascii=False), 'ingested_at': utcnow()}
        updates = ','.join(f"`{x}`=VALUES(`{x}`)" for x in cols if x not in ('id', 'ingested_at'))
        try:
            with self.conn.cursor() as c:
                c.execute("INSERT INTO `doc` (" + ','.join(f"`{x}`" for x in cols) +
                          ") VALUES (" + ','.join(['%s']*len(cols)) + ") ON DUPLICATE KEY UPDATE " + updates,
                          tuple(row[x] for x in cols))
                c.execute("DELETE FROM `doc_image` WHERE `doc_id`=%s", (doc['id'],))
                if images:
                    c.executemany("""INSERT INTO `doc_image`
                        (`doc_id`,`position`,`file_key`,`status`,`width`,`height`) VALUES (%s,%s,%s,%s,%s,%s)""",
                        [tuple(im[x] for x in SCHEMA['doc_image']) for im in images])
            self._commit()
        except Exception:
            self._rollback()
            raise

    def finish(self, run_id, source_id, counts, errors, finished_at=None):
        try:
            with self.conn.cursor() as c:
                c.execute("""UPDATE `source` SET
                    `n_docs`=(SELECT COUNT(*) FROM `doc` WHERE `source_id`=%s),
                    `first_ts`=(SELECT MIN(`published_at`) FROM `doc` WHERE `source_id`=%s),
                    `last_ts`=(SELECT MAX(`published_at`) FROM `doc` WHERE `source_id`=%s)
                    WHERE `id`=%s""", (source_id,)*4)
                c.execute("""UPDATE `collection_run` SET `finished_at`=%s,`n_discovered`=%s,
                    `n_fetched`=%s,`n_failed`=%s,`status`=%s,`error`=%s WHERE `id`=%s""",
                    (finished_at or utcnow(), counts.get('n_index',0), counts.get('n_fetched',0),
                     counts.get('n_failed',0), 'failed' if errors else 'success',
                     '; '.join(errors)[:4000] or None, run_id))
            self._commit()
        except Exception:
            self._rollback()
            raise
