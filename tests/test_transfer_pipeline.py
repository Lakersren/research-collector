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
from aris import archive_source,kb_adapters,web_media,semi_media
from aris.archive_source import export_source, source_result, stamp
from aris.daily_archive import publish_date
from aris.local_images import LocalImages
from aris.sync import Puller,Ledger
from aris.read_data import ArticleReader,read_session
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
        sql=sql.replace(' FOR UPDATE','').replace('%s','?')
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
        self.root=Path(self.temp.name);self.raw=self.root/'raw';self.archive=self.root/'archive'
        self.state=self.root/'B-state';self.data=self.root/'B-data'
        self.objects=Objects();self.tamper=False;self.report_file=self.archive/"20260101/report.json"
        for module in (kb_adapters,web_media,semi_media,archive_source):
            p=patch.object(module,'RAW',self.raw);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'CORPUS_API_TOKEN':ADMIN,'CORPUS_TRANSFER_TOKEN':TOKEN})
        env.start();self.addCleanup(env.stop)
        patcher=patch.object(service,'STATE',self.root/'A-state');patcher.start();self.addCleanup(patcher.stop)
        config=self.root/'schedule.yaml';config.write_text('jobs: []\n')
        dispatcher=Mock();dispatcher.active=None
        self.a=TestClient(service.create_app(config,lambda:dispatcher,archive_root=self.archive))
        self.a.__enter__();self.addCleanup(self.a.__exit__,None,None,None)
        self.dbpath=self.root/'db.sqlite'
        db=sqlite3.connect(self.dbpath)
        with db:
            db.executescript('''
                CREATE TABLE source(id TEXT PRIMARY KEY,name TEXT,channel TEXT,trust INTEGER,note TEXT,n_docs INTEGER,first_ts DATETIME,last_ts DATETIME);
                CREATE TABLE doc(id TEXT PRIMARY KEY,source_id TEXT,title TEXT,authors TEXT,published_at DATETIME,url TEXT,content_state TEXT,content TEXT,ingested_at DATETIME,content_hash BLOB);
                CREATE TABLE doc_label(doc_id TEXT,label_type TEXT,label_value TEXT,is_primary INTEGER,labeled_at DATETIME);
                CREATE TABLE doc_image(doc_id TEXT,position INTEGER,file_key TEXT,status TEXT,width INTEGER,height INTEGER,PRIMARY KEY(doc_id,position));
                CREATE TABLE collection_run(id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,started_at DATETIME,finished_at DATETIME,n_discovered INTEGER,n_fetched INTEGER,n_failed INTEGER,status TEXT,error TEXT);
            ''')
        db.close()
        def handler(request):
            response=self.a.request(request.method,request.url.path+('?' +request.url.query.decode() if request.url.query else ''),
                                    headers={'Authorization':request.headers.get('Authorization','')})
            body=response.content
            if self.tamper and request.url.path.endswith('.txt'):body=body.replace(b'Full',b'FAIL')
            return httpx.Response(response.status_code,content=body,headers=response.headers)
        self.http=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(self.http.close)
    def publish(self,title='Original',image=True,other=False,replace_all=True,status='success'):
        directory=self.raw/'web/semiengineering/items';directory.mkdir(parents=True,exist_ok=True)
        value=dict(iid='one',title=title,date='2026-01-01T08:00:00+08:00',url='https://example.test/one',
                   md='Full body\n![chart](https://cdn.test/a.png)' if image else 'Full body',author='研究员')
        (directory/'one.json').write_text(json.dumps(value),encoding='utf-8')
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','fixture')):
            from aris.image_ingest import sync_images
            self.assertEqual(sync_images(SOURCE,LocalImages(self.raw)),0)
        run.extract(SOURCE)
        started = stamp()
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            stage = Path(tmp)
            saved, files = export_source(SOURCE, stage, 'semiengineering', '2026-01-01')
            sources = [source_result(SOURCE, 'semiengineering', started, saved, files,
                                     {'n_index': 1, 'n_fetched': 1},
                                     errors=['incomplete'] if status in ('partial_success', 'interrupted') else ())]
            if other:
                (stage / 'openai').mkdir()
                sources.append(source_result(dict(key='web:openai', name='OpenAI News'), 'openai', started))
            job = str(uuid.uuid4())
            report = dict(version=1, job_id=job, target_date='2026-01-01', timezone='Asia/Shanghai',
                          execution_date=started[:10], started_at=started, finished_at=stamp(),
                          status=status, sources=sources)
            publish_date(self.root, stage, report, replace_all)
        return job
    def pull(self, target_date='2026-01-01'):
        puller=Puller('http://collector.test',TOKEN,self.http,state=self.state,data=self.data,
                      connection_factory=lambda:SQLConnection(self.dbpath),object_factory=lambda:self.objects)
        return puller.run(target_date)
    def rows(self,table):
        db=sqlite3.connect(self.dbpath)
        try:return db.execute('SELECT * FROM '+table).fetchall()
        finally:db.close()
    def test_round_trip_local_images_sql_and_read_api(self):
        self.publish()
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
    def test_partial_archive_still_imports_articles(self):
        self.publish(status='partial_success')
        self.assertEqual(self.pull(), 1)
        self.assertEqual(len(self.rows('doc')), 1)
        self.assertEqual(self.pull(), 0)

    def test_checksum_failure_never_writes(self):
        self.publish();self.tamper=True
        with self.assertRaises(ValueError):self.pull()
        self.assertEqual(self.rows('doc'),[])
        self.tamper=False;self.assertEqual(self.pull(),1)
    def test_object_failure_rolls_back_and_retry_succeeds(self):
        self.publish();self.objects.fail=True
        with self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(self.rows('doc'),[]);self.assertEqual(self.rows('source'),[])
        self.assertEqual(self.rows('collection_run'),[])
        self.objects.fail=False;self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('collection_run')),1)
    def test_commit_checkpoint_crash_recovers_without_duplicate_run(self):
        self.publish();original=Ledger.set;failed=[False]
        def crash(ledger,batch_id,**fields):
            if fields.get('status')=='done' and not failed[0]:
                failed[0]=True;raise RuntimeError('simulated crash after commit')
            return original(ledger,batch_id,**fields)
        with patch.object(Ledger,'set',crash),self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(len(self.rows('doc')),1)
        self.assertEqual(self.pull(),1);self.assertEqual(len(self.rows('collection_run')),1)
        self.assertEqual(self.objects.uploads,1)
    def test_database_failure_after_article_write_rolls_back_source(self):
        self.publish()
        from aris.kb import Repository
        original=Repository.save_article
        def fail(repo,doc,images):
            original(repo,doc,images)
            raise RuntimeError('injected failure after article write')
        with patch.object(Repository,'save_article',fail),self.assertRaises(RuntimeError):self.pull()
        for table in ('source','doc','doc_image','collection_run'):self.assertEqual(self.rows(table),[])
        self.assertEqual(self.pull(),1)
        self.assertEqual(len(self.rows('doc')),1)
        self.assertEqual(self.objects.uploads,1)

    def test_date_sync_pulls_only_the_current_result(self):
        self.publish('First');self.publish('Second')
        self.objects.fail=True
        with self.assertRaises(RuntimeError):self.pull()
        self.assertEqual(self.rows('doc'),[])
        self.objects.fail=False;self.assertEqual(self.pull('20260101'),1)
        self.assertEqual(self.rows('doc')[0][2],'Second')

    def test_source_mismatch_is_rejected_before_writing(self):
        self.publish()
        value = json.loads(self.report_file.read_text())
        value['sources'][0]['articles']['success'][0]['id'] = 'web:openai:one'
        self.report_file.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'id does not belong'):
            self.pull()
        self.assertEqual(self.rows('source'), [])
        self.assertEqual(self.objects.files, {})

    def test_static_images_supported_active_svg_rejected(self):
        from corpus_contract import image_ext
        self.assertEqual(image_ext(b'BMbitmap'),'bmp')
        self.assertEqual(image_ext(b'II*\x00tiff'),'tiff')
        self.assertEqual(image_ext(b'<svg><rect fill="url(#chart)"/></svg>'),'svg')
        self.assertIsNone(image_ext(b'<svg onload="alert(1)"></svg>'))
        self.assertIsNone(image_ext(b'<svg><script>alert(1)</script></svg>'))
        self.assertIsNone(image_ext(b'<html>not an image</html>'))

    def test_unfinished_and_traversal_are_not_downloadable(self):
        self.publish()
        headers = {'Authorization':'Bearer '+TOKEN}
        path = '/transfer/v1/days/20260101'
        self.assertEqual(self.a.get('/transfer/v1/days/20260102', headers=headers).status_code, 404)
        self.assertEqual(self.a.get(path+'/files/report.json', headers=headers).status_code, 404)
        self.assertEqual(self.a.get(path+'/files/images/%2e%2e/%2e%2e/secret', headers=headers).status_code, 404)
        self.assertEqual(self.a.get(path).status_code, 401)
        self.assertEqual(self.a.get(path, headers={'Authorization':'Bearer '+ADMIN}).status_code, 401)
    def test_collector_cli_needs_no_database_or_storage_environment(self):
        with patch.dict(os.environ, {'ARIS_OVERSEAS_DIRECT':'1'}, clear=True), patch('run.setup_logging'), patch.object(run, 'STATE', self.root/'check-state'), patch.object(run, 'DATA', self.root):
            self.assertEqual(run.main(['check']), 0)

    def test_date_replacement_and_single_source_sync(self):
        self.publish(other=True)
        original = json.loads(self.report_file.read_text())
        archive = self.report_file.parent
        article = original['sources'][0]['articles']['success'][0]
        self.assertIn('[图1]', (archive / article['text_path']).read_text())
        self.assertEqual((archive / article['images'][0]['path']).read_bytes(), PNG)
        self.assertEqual(self.pull(), 2)

        failed = self.publish('Discarded', status='interrupted')
        report = json.loads(self.report_file.read_text())
        self.assertEqual(report['sources'], original['sources'])
        self.assertEqual(report['latest_attempt']['job_id'], failed)
        self.assertEqual(self.pull(), 0)

        self.publish('Updated', replace_all=False)
        self.assertTrue((archive / 'openai').is_dir())
        self.assertEqual(self.pull(), 1)
        docs = self.rows('doc')
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0][2], 'Updated')

        third = self.publish('Replacement')
        self.assertEqual(json.loads(self.report_file.read_text())['job_id'], third)
        self.assertFalse((archive / 'openai').exists())
        self.assertEqual(self.pull(), 1)
        self.assertEqual(self.pull(), 0)

    def test_report_target_date_is_enforced_before_sql(self):
        self.publish()
        path = self.report_file
        report = json.loads(path.read_text())
        report['sources'][0]['articles']['success'][0]['published_at'] = '2026-01-02T00:00:00+08:00'
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'publication date'):
            self.pull()
        self.assertEqual(self.rows('doc'), [])


if __name__ == '__main__':
    unittest.main()
