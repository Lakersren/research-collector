#!/usr/bin/env python3
"""Run the independent label job or backfill hashes after upgrading MySQL."""
import bootstrap
import argparse
import fcntl
import logging
import os

import httpx

from aris.kb import article_content_hash, connect
from aris.labels import Classifier, label_pending
from aris.observability import setup_logging
from aris.paths import STATE

log = logging.getLogger('corpus.processing')


def backfill_hashes(connection):
    last_id = ''
    updated = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,content FROM doc WHERE id>%s AND content_hash IS NULL "
                "AND content_state='ready' AND content IS NOT NULL ORDER BY id LIMIT 100 FOR UPDATE",
                (last_id,),
            )
            rows = cursor.fetchall()
            values = [(article_content_hash('ready', content), doc_id) for doc_id, content in rows]
            values = [(value, doc_id) for value, doc_id in values if value is not None]
            if values:
                cursor.executemany('UPDATE doc SET content_hash=%s WHERE id=%s', values)
        connection.commit()
        if not rows:
            break
        updated += len(values)
        last_id = rows[-1][0]
    log.info('content_hash_backfill_finished updated=%d', updated)


def main():
    parser = argparse.ArgumentParser(description='B 语料加工任务')
    parser.add_argument('--backfill-content-hashes', action='store_true', help='为历史正文补算哈希')
    parser.add_argument('--scheduled', action='store_true', help='模型未配置时跳过定时打标')
    args = parser.parse_args()
    setup_logging('processing')
    if args.scheduled and not args.backfill_content_hashes and not any(
        os.getenv('ARIS_LLM_' + name) for name in ('BASE_URL', 'API_KEY', 'MODEL')
    ):
        log.info('label_run_skipped reason=model_unconfigured')
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = STATE / ('sync.lock' if args.backfill_content_hashes else 'labels.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning('processing_already_running')
            return 1
        try:
            with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
                classifier = None if args.backfill_content_hashes else Classifier.from_env(client)
                connection = connect()
                try:
                    if classifier is None:
                        backfill_hashes(connection)
                        return 0
                    return int(label_pending(connection, classifier) > 0)
                finally:
                    connection.close()
        except Exception as exc:
            log.error('processing_failed error_type=%s', type(exc).__name__)
            if isinstance(exc, ValueError):
                log.error('processing_configuration_invalid')
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
