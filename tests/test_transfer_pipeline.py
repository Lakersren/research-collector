"""A real local batch, authenticated HTTP download, transactional SQL and query round trip."""
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
import unittest
from unittest.mock import Mock,patch

REPO=Path(__file__).resolve().parents[1]
A=REPO/'research-collector';B=REPO/'research-data-service'
os.environ.setdefault('ARIS_ROOT',str(A))
sys.path[:0]=[str(A),str(B),str(A/'lib'),str(B/'lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
import httpx
from fastapi.testclient import TestClient
import service,run
from aris import batches,kb_adapters,web_media,semi_media
from aris.local_images import LocalImages
from aris.sync import Puller,Ledger
from aris.read_data import ArticleReader,read_session
from corpus_contract import RunReport,Manifest,Article,digest
from test_read_services import SQLiteConnection,FakeSigner

TOKEN='transfer-test-token-with-at-least-32-characters'
ADMIN='admin-test-token-with-at-least-32-characters'
PNG=b'\x89PNG\r\n\x1a\nfixture'
SOURCE={'key':'web:semiengineering','name':'Semiconductor Engineering','_seg':'rss'}

class SQLCursor:
    def __init__(self,connection):self.connection=connection;self.cursor=connection.db.cursor();self.lastrowid=None
    def __enter__(self):return self
    def __exit__(self,*args):self.cursor.close()
    def execute(self,sql,params=()):
        sql=sql.replace('%s','?')
        match=re.search(r'INSERT INTO `(source|doc)`',sql)
        if match and 'ON DUPLICATE KEY UPDATE' in sql:
            sql=sql.replace('ON DUPLICATE KEY UPDATE','ON CONFLICT(id) DO UPDATE SET')
            sql=re.sub(r'VALUES\(`(\w+)`\)',r'excluded.`\1`',sql)
        self.cursor.execute(sql,params);self.lastrowid=self.cursor.lastrowid
    def executemany(self,sql,params):self.cursor.executemany(sql.replace('%s','?'),params)
    def fetchone(self):return self.cursor.fetchone()

class SQLConnection:
    def __init__(self,path):self.db=sqlite3.connect(path,detect_types=sqlite3.PARSE_DECLTYPES)
    def cursor(self,*args):return SQLCursor(self)
    def commit(self):self.db.commit()
    def rollback(self):self.db.rollback()
    def close(self):self.db.close()

class Objects:
    def __init__(self):self.files={};self.fail=False;self.client=Mock();self.uploads=0
    def key(self,source,value,ext):return 'overseas-corpus/images/'+hashlib.sha256(value.encode()).hexdigest()+'.'+ext
    def exists(self,key):return key in self.files
    def upload(self,key,body,ctype):
        if self.fail:raise RuntimeError('injected object upload failure')
        self.files[key]=body;self.uploads+=1

class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.raw=self.root/'raw';self.spool=self.root/'batches'
        self.state=self.root/'B-state';self.data=self.root/'B-data'
        self.objects=Objects();self.ack_fail=False;self.tamper=False;self.ack_count=0
        for module in (kb_adapters,web_media,semi_media,batches):
            p=patch.object(module,'RAW',self.raw);p.start();self.addCleanup(p.stop)
        patcher=patch.object(batches,'BATCHES',self.spool);patcher.start();self.addCleanup(patcher.stop)
        env=patch.dict(os.environ,{'CORPUS_API_TOKEN':ADMIN,'CORPUS_TRANSFER_TOKEN':TOKEN})
        env.start();self.addCleanup(env.stop)
        patcher=patch.object(service,'STATE',self.root/'A-state');patcher.start();self.addCleanup(patcher.stop)
        from aris import transfer_api
        patcher=patch.object(transfer_api,'STATE',self.root/'A-state');patcher.start();self.addCleanup(patcher.stop)
        config=self.root/'schedule.yaml';config.write_text('jobs: []\n')
        dispatcher=Mock();dispatcher.active=None
        self.a=TestClient(service.create_app(config,lambda:dispatcher,batch_root=self.spool))
        self.a.__enter__();self.addCleanup(self.a.__exit__,None,None,None)
        self.dbpath=self.root/'db.sqlite'
        db=sqlite3.connect(self.dbpath)
        with db:
            db.executescript('''
                CREATE TABLE source(id TEXT PRIMARY KEY,name TEXT,channel TEXT,trust INTEGER,note TEXT,n_docs INTEGER,first_ts DATETIME,last_ts DATETIME);
                CREATE TABLE doc(id TEXT PRIMARY KEY,source_id TEXT,title TEXT,authors TEXT,published_at DATETIME,url TEXT,content_state TEXT,content TEXT,ingested_at DATETIME);
                CREATE TABLE doc_image(doc_id TEXT,position INTEGER,file_key TEXT,status TEXT,width INTEGER,height INTEGER,PRIMARY KEY(doc_id,position));
                CREATE TABLE collection_run(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,started_at DATETIME,finished_at DATETIME,n_discovered INTEGER,n_fetched INTEGER,n_failed INTEGER,status TEXT,error TEXT);
            ''')
        db.close()
        def handler(request):
            if request.url.path.endswith('/ack'):
                self.ack_count+=1
                if self.ack_fail:return httpx.Response(503,json={'error':'injected ack failure'})
            response=self.a.request(request.method,request.url.path+('?' +request.url.query.decode() if request.url.query else ''),
                                    headers={'Authorization':request.headers.get('Authorization','')})
            body=response.content
            if self.tamper and request.url.path.endswith('/articles.jsonl'):body=body.replace(b'Original',b'TAMPERED')
            return httpx.Response(response.status_code,content=body,headers=response.headers)
        self.http=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(self.http.close)
    def publish(self,title='Original',state='ready',image=True):
        directory=self.raw/'web/semiengineering/items';directory.mkdir(parents=True,exist_ok=True)
        value=dict(iid='one',title=title,date='2026-01-01T08:00:00+08:00',url='https://example.test/one',
                   md='Full body\n![chart](https://cdn.test/a.png)' if image else 'Full body',author='研究员')
        (directory/'one.json').write_text(json.dumps(value),encoding='utf-8')
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','fixture')):
            from aris.image_ingest import sync_images
            self.assertEqual(sync_images(SOURCE,LocalImages(self.raw)),0)
        run.extract(SOURCE)
        manifest=batches.publish(SOURCE,{'n_index':1,'n_fetched':1},[],batches.stamp())
        report=RunReport(id=manifest.id,job_id=str(uuid.uuid4()),target_date='2026-01-01',
            started_at=manifest.collection.started_at,finished_at=manifest.collection.finished_at,status='success',
            sources=[dict(source_id=SOURCE['key'],source_name=SOURCE['name'],batch_id=manifest.id,collection=manifest.collection)])
        directory=self.root/'runs';directory.mkdir(exist_ok=True)
        (directory/(report.id+'.json')).write_text(report.model_dump_json())
        return manifest
    def pull(self):
        puller=Puller('http://collector.test',TOKEN,self.http,state=self.state,data=self.data,
                      connection_factory=lambda:SQLConnection(self.dbpath),object_factory=lambda:self.objects)
        return puller.run()
    def rows(self,table):
        db=sqlite3.connect(self.dbpath)
        try:return db.execute('SELECT * FROM '+table).fetchall()
        finally:db.close()
    def test_round_trip_local_images_sql_and_read_api(self):
        manifest=self.publish()
        self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('source')),1);self.assertEqual(len(self.rows('doc')),1)
        self.assertEqual(len(self.rows('doc_image')),1);self.assertEqual(len(self.rows('collection_run')),1)
        reader=ArticleReader('cursor-test-secret-at-least-32-characters',
            session_factory=lambda:read_session(lambda:SQLiteConnection(self.dbpath,[])),
            source_factory=lambda:[SOURCE],signer_factory=FakeSigner)
        doc=reader.get_article('web:semiengineering:one')
        self.assertIn('[图1]',doc['content']);self.assertEqual(doc['images'][0]['status'],'ok')
        self.assertEqual(next(iter(self.objects.files.values())),PNG)
        self.assertEqual(self.pull(),0);self.assertEqual(len(self.rows('collection_run')),1)
        self.assertEqual(self.objects.uploads,1)
        ingested=self.rows('doc')[0][-1]
        self.publish('Updated')
        self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('doc')),1);self.assertEqual(self.rows('doc')[0][2],'Updated')
        self.assertEqual(self.rows('doc')[0][-1],ingested)
        self.assertEqual(len(self.rows('collection_run')),2)
    def test_checksum_failure_never_writes_or_acknowledges(self):
        self.publish();self.tamper=True
        with self.assertRaises(ValueError):self.pull()
        self.assertEqual(self.rows('doc'),[]);self.assertEqual(self.ack_count,0)
        self.tamper=False;self.assertEqual(self.pull(),1)
    def test_object_failure_rolls_back_and_retry_succeeds(self):
        self.publish();self.objects.fail=True
        with self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(self.rows('doc'),[]);self.assertEqual(self.rows('source'),[])
        self.assertEqual(self.rows('collection_run'),[]);self.assertEqual(self.ack_count,0)
        self.objects.fail=False;self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('collection_run')),1)
    def test_commit_checkpoint_crash_recovers_without_duplicate_run(self):
        self.publish();original=Ledger.set;failed=[False]
        def crash(ledger,batch_id,**fields):
            if fields.get('status')=='done' and not failed[0]:
                failed[0]=True;raise RuntimeError('simulated crash after commit')
            return original(ledger,batch_id,**fields)
        with patch.object(Ledger,'set',crash),self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(len(self.rows('doc')),1);self.assertEqual(self.ack_count,0)
        self.assertEqual(self.pull(),1);self.assertEqual(len(self.rows('collection_run')),1)
        self.assertEqual(self.objects.uploads,1)
    def test_ack_failure_retries_ack_without_reimport(self):
        self.publish();self.ack_fail=True
        with self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(len(self.rows('doc')),1)
        self.ack_fail=False;self.assertEqual(self.pull(),0)
        self.assertEqual(len(self.rows('collection_run')),1)
    def test_database_failure_after_article_write_rolls_back_whole_batch(self):
        self.publish()
        from aris.kb import Repository
        original=Repository.save_article
        def fail(repo,doc,images):
            original(repo,doc,images)
            raise RuntimeError('injected failure after article write')
        with patch.object(Repository,'save_article',fail),self.assertRaises(RuntimeError):self.pull()
        for table in ('source','doc','doc_image','collection_run'):self.assertEqual(self.rows(table),[])
        self.assertEqual(self.ack_count,0)
        self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('doc')),1)
        self.assertEqual(self.objects.uploads,1)

    def test_later_batch_waits_for_earlier_failure(self):
        self.publish('First');self.publish('Second')
        self.objects.fail=True
        with self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(self.rows('doc'),[])
        self.objects.fail=False;self.assertEqual(self.pull(),2)
        self.assertEqual(self.rows('doc')[0][2],'Second')

    def test_source_mismatch_is_rejected_before_writing(self):
        manifest=self.publish()
        directory=self.spool/manifest.id
        p=directory/'articles.jsonl';value=json.loads(p.read_text());value['source_id']='web:openai'
        p.write_text(json.dumps(value)+'\n')
        path=directory/'manifest.json';value=json.loads(path.read_text())
        for entry in value['files']:
            if entry['path']=='articles.jsonl':entry.update(size=p.stat().st_size,sha256=digest(p))
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.pull()
        self.assertEqual(self.rows('source'),[]);self.assertEqual(self.objects.files,{})

    def test_static_images_supported_active_svg_rejected(self):
        from corpus_contract import image_ext
        self.assertEqual(image_ext(b'BMbitmap'),'bmp')
        self.assertEqual(image_ext(b'II*\x00tiff'),'tiff')
        self.assertEqual(image_ext(b'<svg><rect fill="url(#chart)"/></svg>'),'svg')
        self.assertIsNone(image_ext(b'<svg onload="alert(1)"></svg>'))
        self.assertIsNone(image_ext(b'<svg><script>alert(1)</script></svg>'))
        self.assertIsNone(image_ext(b'<html>not an image</html>'))

    def test_unfinished_and_traversal_are_not_downloadable(self):
        manifest=self.publish();(self.spool/'.unfinished').mkdir()
        headers={'Authorization':'Bearer '+TOKEN}
        response=self.a.get('/transfer/v1/batches',headers=headers)
        self.assertEqual(response.json()['items'],[manifest.id])
        path='/transfer/v1/batches/'+manifest.id+'/files/'
        self.assertEqual(self.a.get(path+'manifest.json',headers=headers).status_code,404)
        self.assertEqual(self.a.get(path+'images/%2e%2e/%2e%2e/secret',headers=headers).status_code,404)
        self.assertEqual(self.a.get('/transfer/v1/batches').status_code,401)
        self.assertEqual(self.a.get('/transfer/v1/batches',headers={'Authorization':'Bearer '+ADMIN}).status_code,401)
    def test_unknown_contract_version_is_rejected(self):
        manifest=self.publish();p=self.spool/manifest.id/'manifest.json'
        value=json.loads(p.read_text());value['version']=2;p.write_text(json.dumps(value))
        with self.assertRaises(Exception):self.pull()
        self.assertEqual(self.rows('doc'),[])
    def test_collector_cli_needs_no_database_or_storage_environment(self):
        with patch.dict(os.environ,{'ARIS_OVERSEAS_DIRECT':'1'},clear=True),patch('run.setup_logging'),patch.object(run,'STATE',self.root/'check-state'),patch.object(run,'BATCHES',self.spool):
            self.assertEqual(run.main(['check']),0)

if __name__=='__main__':unittest.main()

class DailyReportTransferTests(unittest.TestCase):
    setUp=TransferTests.setUp
    publish=TransferTests.publish
    pull=TransferTests.pull
    rows=TransferTests.rows
    """Use the same real HTTP/SQL harness for new report-gated behavior."""
    def test_batch_without_terminal_report_is_not_imported(self):
        manifest=self.publish()
        (self.root/'runs'/(manifest.id+'.json')).unlink()
        self.assertEqual(self.pull(),0)
        self.assertEqual(self.rows('doc'),[]);self.assertEqual(self.ack_count,0)
    def test_local_audit_details_do_not_change_transfer_payload(self):
        manifest = self.publish()
        self.assertEqual(self.pull(), 1)
        path = self.root / 'runs' / (manifest.id + '.json')
        report = json.loads(path.read_text())
        report['sources'][0]['articles'] = {'success': [], 'failed': [], 'skipped': []}
        path.write_text(json.dumps(report))
        self.assertEqual(self.pull(), 0)
        self.assertEqual(len(self.rows('doc')), 1)
    def test_interrupted_source_without_batch_is_recorded_once(self):
        from aris.daily_runs import begin,finish
        path=begin(str(uuid.uuid4()),[SOURCE],self.root,target_date='2026-01-01')
        report=finish(path,interrupted=True)
        self.assertEqual(self.pull(),1)
        self.assertEqual(self.rows('doc'),[])
        rows=self.rows('collection_run');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][-2],'failed')
        self.assertEqual(self.ack_count,0)
        self.assertEqual(self.pull(),0);self.assertEqual(len(self.rows('collection_run')),1)
        self.assertTrue((self.data/'incoming/runs'/(report.id+'.json')).is_file())
    def test_report_target_date_is_enforced_before_sql(self):
        manifest=self.publish()
        path=self.root/'runs'/(manifest.id+'.json');report=json.loads(path.read_text())
        report['target_date']='2026-01-02';path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'publication date'):self.pull()
        self.assertEqual(self.rows('doc'),[]);self.assertEqual(self.rows('collection_run'),[])
    def test_report_manifest_mismatch_is_not_imported(self):
        manifest=self.publish()
        path=self.root/'runs'/(manifest.id+'.json');report=json.loads(path.read_text())
        report['sources'][0]['collection']['n_fetched']=999;path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError,'disagree'):self.pull()
        self.assertEqual(self.rows('doc'),[])
    def test_zero_articles_is_a_successful_record_not_a_missing_run(self):
        manifest=self.publish(image=False);directory=self.spool/manifest.id
        article=directory/'articles.jsonl';article.write_bytes(b'\n')
        value=manifest.model_dump();value['collection'].update(n_discovered=0,n_fetched=0)
        value['files']=[dict(path='articles.jsonl',size=1,sha256=digest(article))]
        manifest=Manifest.model_validate(value)
        (directory/'manifest.json').write_text(manifest.model_dump_json())
        path=self.root/'runs'/(manifest.id+'.json');report=json.loads(path.read_text())
        report['sources'][0]['collection']=manifest.collection.model_dump();path.write_text(json.dumps(report))
        self.assertEqual(self.pull(),1);self.assertEqual(self.rows('doc'),[])
        self.assertEqual(self.rows('collection_run')[0][-2],'success')
