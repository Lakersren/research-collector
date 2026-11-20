"""Read the published date directory and checkpoint committed source imports."""
from datetime import datetime,timezone
import fcntl
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit,quote
import httpx
from corpus_contract import (DailyResult, digest, MAX_MANIFEST_BYTES)
from aris.daily_policy import matches, normalize_target_date, yesterday
from aris.paths import DATA,STATE
from aris.kb import Repository,connect
from aris.object_store import ObjectStore
from aris.source_config import sources
from aris.observability import log_context

log=logging.getLogger('corpus.sync')

def dt(value): return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc).replace(tzinfo=None)

class Ledger:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS source_revision (id TEXT PRIMARY KEY, digest TEXT NOT NULL, run_id INTEGER, status TEXT NOT NULL, error TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS day (target_date TEXT PRIMARY KEY, digest TEXT NOT NULL)')
        self.db.commit()
        self.db.row_factory=sqlite3.Row
    def get(self,revision):
        row=self.db.execute('SELECT * FROM source_revision WHERE id=?',(revision,)).fetchone()
        return dict(row) if row else None
    def prepare(self,revision,checksum):
        row=self.get(revision)
        if row and row['digest']!=checksum: raise ValueError('source revision changed')
        self.db.execute('INSERT OR IGNORE INTO source_revision(id,digest,status) VALUES(?,?,?)',(revision,checksum,'pending'));self.db.commit()
    def set(self,revision,**fields):
        if not fields or set(fields)-{'run_id','status','error'}: raise ValueError('invalid ledger field')
        self.db.execute('UPDATE source_revision SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),revision));self.db.commit()
    def day_is_current(self, target_date, checksum):
        row = self.db.execute('SELECT digest FROM day WHERE target_date=?', (target_date,)).fetchone()
        return row is not None and row['digest'] == checksum
    def finish_day(self, target_date, checksum):
        self.db.execute('INSERT OR REPLACE INTO day(target_date,digest) VALUES(?,?)', (target_date, checksum))
        self.db.commit()
    def close(self):self.db.close()

class Puller:
    def __init__(self,base=None,token=None,client=None,state=None,data=None,connection_factory=connect,object_factory=ObjectStore):
        self.base=(base or os.getenv('CORPUS_COLLECTOR_URL','')).rstrip('/')
        parsed=urlsplit(self.base)
        if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('CORPUS_COLLECTOR_URL 必须是固定服务根地址')
        self.token=token or os.getenv('CORPUS_TRANSFER_TOKEN','')
        if len(self.token)<24:raise ValueError('同步令牌至少 24 字符')
        self.client=client or httpx.Client(timeout=httpx.Timeout(60,connect=10),trust_env=False,follow_redirects=False)
        self.owns_client=client is None
        self.target_date=None
        self.state=Path(state or STATE);self.data=Path(data or DATA)/'incoming'
        self.connection_factory,self.object_factory=connection_factory,object_factory
        self.source_map={s['key']:s for s in sources()}
    def bounded_json(self,path,max_size):
        with self.client.stream('GET',self.base+'/transfer/v1'+path,
                headers={'Authorization':'Bearer '+self.token}) as response:
            if response.status_code!=200:raise RuntimeError(f'collector HTTP {response.status_code}')
            body=bytearray()
            for chunk in response.iter_bytes(65536):
                body.extend(chunk)
                if len(body)>max_size:raise ValueError('metadata exceeds limit')
        return bytes(body)
    def download(self, source, directory):
        directory.mkdir(parents=True,exist_ok=True)
        for entry in source.files:
            dest=directory/entry.path
            if dest.is_file() and dest.stat().st_size==entry.size and digest(dest)==entry.sha256:continue
            dest.parent.mkdir(parents=True,exist_ok=True)
            tmp=dest.with_suffix(dest.suffix+'.part')
            try:
                with self.client.stream('GET',self.base+'/transfer/v1/days/'+self.target_date+'/files/'+quote(entry.path,safe='/'),
                    headers={'Authorization':'Bearer '+self.token}) as response:
                    if response.status_code!=200:raise RuntimeError(f'download HTTP {response.status_code}')
                    size=0;hasher=hashlib.sha256()
                    with tmp.open('wb') as out:
                        for chunk in response.iter_bytes(1024*1024):
                            size+=len(chunk)
                            if size>entry.size:raise ValueError('file larger than report')
                            out.write(chunk);hasher.update(chunk)
                    if size!=entry.size or hasher.hexdigest()!=entry.sha256:raise ValueError('file checksum mismatch')
                tmp.replace(dest)
            finally:tmp.unlink(missing_ok=True)
    def validate_source(self, source, directory):
        prefix = 'semianalysis:' if source.source_id == 'web:semianalysis' else source.source_id + ':'
        for article in source.articles:
            if not matches(article.published_at, self.target_date):
                raise ValueError('article publication date outside requested date')
            if not article.id.startswith(prefix):
                raise ValueError('article id does not belong to source')
            if article.text_path and not (directory / article.text_path).read_text(encoding='utf-8').strip():
                raise ValueError('archived body is empty')
            for image in article.images:
                if image.path and not image_type((directory / image.path).read_bytes(),
                                                 Path(image.path).suffix.lower()):
                    raise ValueError('unsupported or inconsistent image')

    def import_source(self, source, directory, ledger, revision):
        self.validate_source(source, directory)
        row = ledger.get(revision)
        conn=self.connection_factory();objects=None
        try:
            repo=Repository(conn,commit_each=False)
            if row['run_id'] is not None:
                with conn.cursor() as c:
                    c.execute('SELECT source_id,status,started_at,finished_at FROM collection_run WHERE id=%s',(row['run_id'],))
                    existing=c.fetchone()
                if existing:
                    if (existing[0]!=source.source_id or existing[1] not in ('success','failed')
                        or existing[2]!=dt(source.collection.started_at)
                        or existing[3]!=dt(source.collection.finished_at)):
                        raise ValueError('reserved collection run conflicts')
                    # Previous MySQL commit succeeded but the local completion checkpoint did not.
                    ledger.set(revision,status='done',error=None)
                    return
            run_id=repo.start(self.source_map[source.source_id],run_id=row['run_id'],started_at=dt(source.collection.started_at))
            # Save reserved AUTO_INCREMENT id before MySQL commit. Retry can safely reuse it after rollback.
            ledger.set(revision,run_id=run_id,status='importing',error=None)
            if any(image.path for article in source.articles for image in article.images):
                objects=self.object_factory()
            entries={x.path:x for x in source.files}
            for article in source.articles:
                doc=article.model_dump(exclude={'images', 'text_path'})
                doc['source_id'] = source.source_id
                doc['content'] = (directory / article.text_path).read_text(encoding='utf-8') if article.text_path else None
                doc['published_at']=dt(article.published_at) if article.published_at else None
                images=[]
                for im in article.images:
                    key=None
                    if im.status=='ok':
                        entry=entries[im.path]
                        suffix=Path(im.path).suffix.lower()
                        # Only inert supported images are accepted; HTML/SVG and arbitrary files are refused.
                        body=(directory/im.path).read_bytes()
                        if not image_type(body,suffix):raise ValueError('unsupported or inconsistent image')
                        key=objects.key(source.source_id,entry.sha256,suffix.lstrip('.'))
                        if not objects.exists(key):objects.upload(key,body,mimetypes.guess_type('x'+suffix)[0] or 'application/octet-stream')
                    images.append(dict(doc_id=article.id,position=im.position,file_key=key,status=im.status,width=im.width,height=im.height))
                repo.save_article(doc,images)
            collection=source.collection
            repo.finish(run_id,source.source_id,
                dict(n_index=collection.n_discovered,n_fetched=collection.n_fetched,n_failed=collection.n_failed),
                [collection.error or '外网采集有失败'] if collection.status=='failed' else [],finished_at=dt(collection.finished_at))
            conn.commit()
            ledger.set(revision,status='done',error=None)
        except BaseException:
            conn.rollback();raise
        finally:
            conn.close()
            if objects is not None:objects.client.close()
    def sync_source(self, source, ledger):
        checksum = hashlib.sha256(source.model_dump_json().encode()).hexdigest()
        revision = f'{self.target_date}/{source.source_id}/{checksum}'
        row = ledger.get(revision)
        if row and row['status'] == 'done':
            return 0
        ledger.prepare(revision, checksum)
        with TemporaryDirectory(prefix='.sync-', dir=self.data) as workspace:
            directory = Path(workspace)
            self.download(source, directory)
            self.import_source(source, directory, ledger, revision)
        return 1

    def run(self, target_date=None):
        self.state.mkdir(parents=True,exist_ok=True);self.data.mkdir(parents=True,exist_ok=True)
        try:
            target_date = yesterday() if target_date is None else normalize_target_date(target_date)
            with (self.state/'sync.lock').open('w') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise RuntimeError('已有同步任务运行') from None
                body = self.bounded_json('/days/' + target_date, MAX_MANIFEST_BYTES)
                result = DailyResult.model_validate_json(body)
                if result.target_date != target_date:
                    raise ValueError('daily result target date mismatch')
                if any(entry.source_id not in self.source_map for entry in result.sources):
                    raise ValueError('unknown source')
                directory = self.data / 'days'
                directory.mkdir(exist_ok=True)
                saved = directory / (target_date.replace('-', '') + '.json')
                checksum = hashlib.sha256(body).hexdigest()
                ledger = Ledger(self.state/'sync.sqlite')
                completed = 0
                self.target_date = target_date
                try:
                    if ledger.day_is_current(target_date, checksum) and saved.is_file():
                        log.info('daily_sync_up_to_date target_date=%s', target_date)
                        return 0
                    for entry in result.sources:
                        with log_context(source_id=entry.source_id):
                            completed += self.sync_source(entry, ledger)
                    temporary = saved.with_suffix('.tmp')
                    temporary.write_bytes(body)
                    temporary.replace(saved)
                    ledger.finish_day(target_date, checksum)
                    log.info('daily_sync_completed target_date=%s collection_status=%s imported_sources=%s',
                             target_date, result.status, completed)
                    return completed
                finally:
                    self.target_date = None
                    ledger.close()
        finally:
            if self.owns_client:self.client.close()

def image_type(body,suffix):
    from corpus_contract import image_ext
    return image_ext(body)==suffix.lstrip('.').replace('jpeg','jpg')
