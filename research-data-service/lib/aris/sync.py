"""Pull immutable batches, validate before writes and checkpoint committed imports."""
from contextlib import contextmanager
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit,quote
import httpx
from corpus_contract import (RunReport,Manifest,Article,digest,MAX_MANIFEST_BYTES,MAX_ARTICLE_LINE_BYTES)
from aris.daily_policy import matches
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
        self.db.execute('CREATE TABLE IF NOT EXISTS batch (id TEXT PRIMARY KEY, digest TEXT NOT NULL, run_id INTEGER, status TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0, error TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS report (id TEXT PRIMARY KEY,digest TEXT NOT NULL,status TEXT NOT NULL)')
        self.db.commit()
        self.db.row_factory=sqlite3.Row
    def get(self,batch_id):
        row=self.db.execute('SELECT * FROM batch WHERE id=?',(batch_id,)).fetchone()
        return dict(row) if row else None
    def prepare(self,batch_id,checksum):
        row=self.get(batch_id)
        if row and row['digest']!=checksum: raise ValueError('immutable manifest changed')
        self.db.execute('INSERT OR IGNORE INTO batch(id,digest,status) VALUES(?,?,?)',(batch_id,checksum,'pending'));self.db.commit()
    def set(self,batch_id,**fields):
        if not fields or set(fields)-{'run_id','status','ack','error'}: raise ValueError('invalid ledger field')
        self.db.execute('UPDATE batch SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),batch_id));self.db.commit()
    def prepare_report(self,ident,checksum):
        row=self.db.execute('SELECT digest,status FROM report WHERE id=?',(ident,)).fetchone()
        if row and row['digest']!=checksum:raise ValueError('immutable run report changed')
        self.db.execute('INSERT OR IGNORE INTO report(id,digest,status) VALUES(?,?,?)',(ident,checksum,'pending'));self.db.commit()
        return bool(row and row['status']=='done')
    def finish_report(self,ident):
        self.db.execute("UPDATE report SET status='done' WHERE id=?",(ident,));self.db.commit()
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
    def request(self,method,path,**kwargs):
        response=self.client.request(method,self.base+'/transfer/v1'+path,
            headers={'Authorization':'Bearer '+self.token},**kwargs)
        if response.status_code!=200:raise RuntimeError(f'collector HTTP {response.status_code}')
        return response
    def bounded_json(self,path,max_size):
        with self.client.stream('GET',self.base+'/transfer/v1'+path,
                headers={'Authorization':'Bearer '+self.token}) as response:
            if response.status_code!=200:raise RuntimeError(f'collector HTTP {response.status_code}')
            body=bytearray()
            for chunk in response.iter_bytes(65536):
                body.extend(chunk)
                if len(body)>max_size:raise ValueError('metadata exceeds limit')
        return bytes(body)
    def download(self,manifest,directory):
        directory.mkdir(parents=True,exist_ok=True)
        for entry in manifest.files:
            dest=directory/entry.path
            if dest.is_file() and dest.stat().st_size==entry.size and digest(dest)==entry.sha256:continue
            dest.parent.mkdir(parents=True,exist_ok=True)
            tmp=dest.with_suffix(dest.suffix+'.part')
            try:
                with self.client.stream('GET',self.base+'/transfer/v1/batches/'+manifest.id+'/files/'+quote(entry.path,safe='/'),
                    headers={'Authorization':'Bearer '+self.token}) as response:
                    if response.status_code!=200:raise RuntimeError(f'download HTTP {response.status_code}')
                    size=0;hasher=hashlib.sha256()
                    with tmp.open('wb') as out:
                        for chunk in response.iter_bytes(1024*1024):
                            size+=len(chunk)
                            if size>entry.size:raise ValueError('file larger than manifest')
                            out.write(chunk);hasher.update(chunk)
                    if size!=entry.size or hasher.hexdigest()!=entry.sha256:raise ValueError('file checksum mismatch')
                tmp.replace(dest)
            finally:tmp.unlink(missing_ok=True)
    def articles(self,manifest,directory):
        files={x.path for x in manifest.files}
        seen=set()
        with (directory/'articles.jsonl').open('rb') as stream:
            while True:
                line=stream.readline(MAX_ARTICLE_LINE_BYTES+1)
                if not line:break
                if len(line)>MAX_ARTICLE_LINE_BYTES:raise ValueError('article exceeds limit')
                if not line.strip():continue
                article=Article.model_validate_json(line)
                if self.target_date and not matches(article.published_at,self.target_date):raise ValueError('article publication date outside run target')
                if article.source_id!=manifest.source_id or article.id in seen:raise ValueError('article source mismatch or duplicate id')
                prefix='semianalysis:' if article.source_id=='web:semianalysis' else article.source_id+':'
                if not article.id.startswith(prefix):raise ValueError('article id does not belong to source')
                seen.add(article.id)
                if any(im.path and im.path not in files for im in article.images):raise ValueError('image missing from manifest')
                yield article
    def import_batch(self,manifest,directory,ledger):
        # Validate all rows before connecting to production dependencies. Re-read for bounded memory.
        for article in self.articles(manifest,directory):
            for im in article.images:
                if im.path and not image_type((directory/im.path).read_bytes(),Path(im.path).suffix.lower()):
                    raise ValueError('unsupported or inconsistent image')
        row=ledger.get(manifest.id)
        conn=self.connection_factory();objects=None
        try:
            repo=Repository(conn,commit_each=False)
            if row['run_id'] is not None:
                with conn.cursor() as c:
                    c.execute('SELECT source_id,status,started_at,finished_at FROM collection_run WHERE id=%s',(row['run_id'],))
                    existing=c.fetchone()
                if existing:
                    if (existing[0]!=manifest.source_id or existing[1] not in ('success','failed')
                        or existing[2]!=dt(manifest.collection.started_at)
                        or existing[3]!=dt(manifest.collection.finished_at)):
                        raise ValueError('reserved collection run conflicts')
                    # Previous MySQL commit succeeded but the local completion checkpoint did not.
                    ledger.set(manifest.id,status='done',error=None)
                    return
            run_id=repo.start(self.source_map[manifest.source_id],run_id=row['run_id'],started_at=dt(manifest.collection.started_at))
            # Save reserved AUTO_INCREMENT id before MySQL commit. Retry can safely reuse it after rollback.
            ledger.set(manifest.id,run_id=run_id,status='importing',error=None)
            objects=self.object_factory()
            entries={x.path:x for x in manifest.files}
            for article in self.articles(manifest,directory):
                doc=article.model_dump(exclude={'images'})
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
                        key=objects.key(manifest.source_id,entry.sha256,suffix.lstrip('.'))
                        if not objects.exists(key):objects.upload(key,body,mimetypes.guess_type('x'+suffix)[0] or 'application/octet-stream')
                    images.append(dict(doc_id=article.id,position=im.position,file_key=key,status=im.status,width=im.width,height=im.height))
                repo.save_article(doc,images)
            collection=manifest.collection
            repo.finish(run_id,manifest.source_id,
                dict(n_index=collection.n_discovered,n_fetched=collection.n_fetched,n_failed=collection.n_failed),
                [collection.error or '外网采集有失败'] if collection.status=='failed' else [],finished_at=dt(collection.finished_at))
            conn.commit()
            ledger.set(manifest.id,status='done',error=None)
        except BaseException:
            conn.rollback();raise
        finally:
            conn.close()
            if objects is not None:objects.client.close()
    def sync_source(self,entry,report,ledger):
        batch_id=entry.batch_id
        synthetic=batch_id is None
        if synthetic:
            # Stable local status-only batch: a failed source may have no article package.
            batch_id=report.id[:20]+'-'+hashlib.sha256((report.id+'/'+entry.source_id).encode()).hexdigest()[:32]
            directory=self.data/batch_id;directory.mkdir(parents=True,exist_ok=True)
            article_file=directory/'articles.jsonl';article_file.write_bytes(b'\n')
            manifest=Manifest(id=batch_id,source_id=entry.source_id,created_at=report.finished_at,
                collection=entry.collection,files=[dict(path='articles.jsonl',size=1,sha256=digest(article_file))])
            body=manifest.model_dump_json().encode()
        else:
            directory=self.data/batch_id
            body=self.bounded_json('/batches/'+batch_id,MAX_MANIFEST_BYTES)
            manifest=Manifest.model_validate_json(body)
            if manifest.id!=batch_id or manifest.source_id!=entry.source_id or manifest.collection!=entry.collection:
                raise ValueError('run report and batch disagree')
        row=ledger.get(batch_id)
        # Always validate metadata, even after an interrupted post-commit acknowledgment.
        ledger.prepare(batch_id,hashlib.sha256(body).hexdigest())
        completed=0
        if not row or row['status']!='done':
            if not synthetic:self.download(manifest,directory)
            self.import_batch(manifest,directory,ledger)
            completed=1
        if not synthetic and not (row and row['ack']):
            ack=self.request('POST','/batches/'+batch_id+'/ack').json()
            if ack.get('id')!=batch_id or ack.get('acknowledged') is not True:raise ValueError('invalid acknowledgment')
        ledger.set(batch_id,ack=1,error=None)
        log.info('batch_sync_completed batch_id=%s source_id=%s collection_status=%s',batch_id,entry.source_id,entry.collection.status)
        return completed

    def run(self):
        self.state.mkdir(parents=True,exist_ok=True);self.data.mkdir(parents=True,exist_ok=True)
        try:
            with (self.state/'sync.lock').open('w') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise RuntimeError('已有同步任务运行') from None
                ledger=Ledger(self.state/'sync.sqlite')
                completed=0
                try:
                    after=''
                    while True:
                        page=json.loads(self.bounded_json('/runs?limit=100&after='+after,65536))
                        if page.get('version')!=1 or not isinstance(page.get('items'),list) or len(page['items'])>100:raise ValueError('invalid run listing')
                        ids=page['items']
                        if any(not isinstance(x,str) for x in ids) or ids!=sorted(set(ids)) or any(x<=after for x in ids):raise ValueError('invalid run ordering')
                        for run_id in ids:
                            import re
                            if not re.fullmatch(r'\d{20}-[0-9a-f]{32}',run_id):raise ValueError('invalid run id')
                            with log_context(run_id=run_id):
                                body=self.bounded_json('/runs/'+run_id,MAX_MANIFEST_BYTES)
                                report=RunReport.model_validate_json(body)
                                if report.id!=run_id or any(x.source_id not in self.source_map for x in report.sources):raise ValueError('unknown run/source')
                                if ledger.prepare_report(run_id,hashlib.sha256(body).hexdigest()):continue
                                self.target_date=report.target_date
                                try:
                                    for entry in report.sources:
                                        with log_context(source_id=entry.source_id,batch_id=entry.batch_id):
                                            completed+=self.sync_source(entry,report,ledger)
                                    # Retain the exact remote report for operational audit, then checkpoint.
                                    directory=self.data/'runs';directory.mkdir(exist_ok=True)
                                    target=directory/(run_id+'.json');temp=target.with_suffix('.tmp')
                                    temp.write_bytes(body);temp.replace(target)
                                    ledger.finish_report(run_id)
                                    log.info('run_sync_completed collection_status=%s target_date=%s',report.status,report.target_date)
                                except Exception:
                                    log.exception('run_sync_failed')
                                    raise
                                finally:self.target_date=None
                        if not page.get('has_more'):break
                        if not ids or page.get('next_cursor')!=ids[-1]:raise ValueError('invalid listing cursor')
                        after=ids[-1]
                    log.info('sync_finished imported_batches=%s',completed)
                    return completed
                finally:ledger.close()
        finally:
            if self.owns_client:self.client.close()

def image_type(body,suffix):
    from corpus_contract import image_ext
    return image_ext(body)==suffix.lstrip('.').replace('jpeg','jpg')
