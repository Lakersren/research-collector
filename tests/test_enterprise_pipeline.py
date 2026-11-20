"""Offline integration: real parsers/adapters, fake remote image and DB/S3 services."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'research-collector'
os.environ.setdefault('ARIS_ROOT',str(ROOT))
sys.path[:0]=[str(ROOT),str(ROOT/'lib'),str(REPO/'research-data-service'),str(REPO/'research-data-service/lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
from aris import kb, kb_adapters as adapters, web_media as wm, semi_media as sm
from aris.image_ingest import sync_images
from aris.object_store import ObjectStore
from botocore.exceptions import ClientError
def ingest(source,repository):
    count=failed=0
    for doc,images in adapters.iter_source(source):
        repository.save_article(doc,images)
        count+=1;failed+=sum(im['status']!='ok' for im in images)
    return count,failed
import run

PNG=b'\x89PNG\r\n\x1a\nfixture'

class MemoryObjects:
    def __init__(self):
        self.files={}
        self.uploads=0
        self.fail=False
    def key(self,source,key,ext):
        return ObjectStore(client=Mock()).key(source,key,ext)
    def exists(self,key):
        return key in self.files
    def upload(self,key,body,ctype):
        if self.fail:
            raise RuntimeError('simulated failure')
        self.files[key]=body
        self.uploads+=1

class Cursor:
    def __init__(self,conn):
        self.conn=conn
        self.lastrowid=42
        self.table=None
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execute(self,sql,params=None):
        self.conn.calls.append((sql,params))
        if sql.startswith('SHOW COLUMNS'):
            self.table=sql.split('`')[1]
    def executemany(self,sql,rows):
        self.conn.calls.append((sql,rows))
        if self.conn.fail_images:
            raise RuntimeError('failed image insert')
    def fetchall(self):
        return [(x,) for x in kb.SCHEMA[self.table] if x != self.conn.missing]

class Connection:
    def __init__(self):
        self.calls=[]
        self.commits=0
        self.rollbacks=0
        self.fail_images=False
        self.missing=None
    def cursor(self): return Cursor(self)
    def commit(self): self.commits+=1
    def rollback(self): self.rollbacks+=1

class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw=Path(self.temp.name)/'raw'
        from collectors import semianalysis as semi
        for module,name,value in [(wm,'RAW',self.raw),(adapters,'RAW',self.raw),(sm,'RAW',self.raw),
                                  (sm,'BASE',self.raw/'web/semianalysis'),(sm,'LEDGER',self.raw/'web/semianalysis/media/index.jsonl'),(semi,'BASE',self.raw/'web/semianalysis')]:
            p=patch.object(module,name,value)
            p.start(); self.addCleanup(p.stop)
        self.objects=MemoryObjects()
        self.source={'key':'web:probe','name':'Probe','_seg':'rss'}
        self.conn=Connection()
        self.repo=kb.Repository(self.conn)
    def put(self,relative,value):
        p=self.raw/relative
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    def rss(self):
        self.put('web/probe/items/one.json',dict(iid='one',title='中文标题',date='2026-01-01T08:00:00+08:00',
            url='https://example.test/a',md='文章正文\n![chart](https://cdn.test/a.png)',author='研究员'))
    def test_rss_whole_chain_and_unchanged_retry(self):
        self.rss()
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','test')) as download:
            self.assertEqual(sync_images(self.source,self.objects),0)
            run.extract(self.source)
            docs=list(adapters.iter_source(self.source))
            self.assertEqual(len(docs),1)
            doc,images=docs[0]
            self.assertEqual(doc['id'],'web:probe:one')
            self.assertEqual(doc['published_at'],datetime(2026,1,1))
            self.assertEqual(doc['authors'],['研究员'])
            self.assertIn('[图1]',doc['content'])
            self.assertEqual(images[0]['position'],1)
            self.assertEqual(images[0]['status'],'ok')
            self.assertIn(images[0]['file_key'],self.objects.files)
            self.assertNotIn('source_url',images[0])
            self.assertEqual(ingest(self.source,self.repo),(1,0))
            self.assertEqual(sync_images(self.source,self.objects),0)
            self.assertEqual(download.call_count,1)
            self.assertEqual(self.objects.uploads,1)
            # Final text and image locations are complete DB/object values, not local paths.
            self.assertNotIn('body_path',doc)
            self.assertEqual(self.conn.commits,1)
    def test_failed_image_is_linked_and_recovered_without_new_articles(self):
        self.rss()
        self.objects.fail=True
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','test')):
            self.assertEqual(sync_images(self.source,self.objects),1)
            run.extract(self.source)
            doc,images=next(adapters.iter_source(self.source))
            self.assertIsNone(images[0]['file_key'])
            self.assertEqual(images[0]['status'],'failed')
            self.assertIn('[图1]',doc['content'])
            self.objects.fail=False
            args=argparse.Namespace(command='collect')
            with patch('run.collect',return_value={'n_index':1,'n_fetched':0,'n_failed':0}):
                self.assertEqual(sync_images(self.source,self.objects),0)
            doc,images=next(adapters.iter_source(self.source))
            self.assertEqual(images[0]['status'],'ok')
            self.assertEqual(ingest(self.source,self.repo),(1,0))
    def test_failed_image_marks_run_failed_but_saves_article(self):
        self.rss()
        self.objects.fail=True
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','test')):
            self.assertEqual(sync_images(self.source,self.objects),1)
            run.extract(self.source)
            ingest(self.source,self.repo)
        self.assertTrue(any(sql.startswith('INSERT INTO `doc`') for sql,_ in self.conn.calls))
        self.assertEqual(next(adapters.iter_source(self.source))[1][0]['status'],'failed')
    def test_substack_ready_preview_and_no_text(self):
        source={**self.source,'_seg':'substack'}
        for number,audience,body in [(1,'everyone','<p>Public article.</p>'),(2,'only_paid','<p>Partial.</p>'),(3,'only_paid',None)]:
            self.put(f'web/probe/posts/{number}.json',dict(id=number,slug=str(number),canonical_url=f'https://example.test/{number}',
                audience=audience,body_html=body,wordcount=100,title=str(number),post_date='2026-01-01T00:00:00Z'))
        run.extract(source)
        rows=list(adapters.iter_source(source))
        self.assertEqual([d['content_state'] for d,_ in rows],['ready','preview','no_text'])
        self.assertIsNone(rows[-1][0]['content'])
    def test_epoch_official_is_ingested(self):
        source={'key':'web:epoch_site','name':'Epoch','_seg':'standalone'}
        self.put('web/epoch_site/items/e.json',dict(iid='e',url='https://epoch.ai/e',date='2026-01-01',title='Epoch',md='Official body.'))
        run.extract(source)
        doc,_=next(adapters.iter_source(source))
        self.assertEqual(doc['id'],'web:epoch_site:e')
        self.assertEqual(doc['content_state'],'ready')
    def test_semianalysis_two_channels_and_images(self):
        source={'key':'web:semianalysis','name':'SemiAnalysis','_seg':'semi'}
        self.put('web/semianalysis/substack/posts/s.json',dict(id=1,slug='s',canonical_url='https://semianalysis.com/s',
            body_html='<p>Public body</p><img src="https://cdn.test/a.png">',audience='everyone',title='Substack',post_date='2026-01-01'))
        self.put('web/semianalysis/wp/posts/1.json',dict(id=1,slug='wp',link='https://semianalysis.com/wp',
            content={'rendered':'<p>Legacy body</p>'},title={'rendered':'WP'},date='2026-01-01'))
        with patch('collectors.web_media_dl.try_one',return_value=(PNG,'image/png','test')):
            self.assertEqual(sync_images(source,self.objects),0)
        run.extract(source)
        rows=list(adapters.iter_source(source))
        self.assertEqual({d['id'] for d,_ in rows},{'semianalysis:substack:1','semianalysis:wp:1'})
        self.assertEqual(sum(len(images) for _,images in rows),1)
        manifest=list(adapters.jsonl(self.raw/'web/semianalysis/text/index.jsonl'))
        self.assertTrue(any(d.get('n_images_stored')==1 for d in manifest))
        self.assertTrue(any(images and images[0]['status']=='ok' for _,images in rows))
    def test_corrupt_json_fails_instead_of_silent_skipping(self):
        p=self.raw/'web/probe/items/bad.json'
        p.parent.mkdir(parents=True)
        p.write_text('{broken')
        with self.assertRaises(ValueError): sync_images(self.source,self.objects)
        with self.assertRaises(ValueError): run.extract(self.source)
    def test_missing_text_cannot_overwrite_existing_article(self):
        p=self.raw/'web/probe/text/index.jsonl'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(post_id=1,url='https://example.test',chars=100,content_state='ready'))+'\n')
        with self.assertRaises(ValueError): list(adapters.iter_source(self.source))
    def test_text_path_traversal_is_rejected(self):
        p=self.raw/'web/probe/text/index.jsonl'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(post_id=1,url='https://example.test',text_path='../../secret'))+'\n')
        with self.assertRaises(ValueError): list(adapters.iter_source(self.source))
    def test_article_image_transaction_rolls_back(self):
        self.rss()
        run.extract(self.source)
        doc,images=next(adapters.iter_source(self.source))
        self.conn.fail_images=True
        with self.assertRaises(RuntimeError): self.repo.save_article(doc,images)
        self.assertEqual(self.conn.rollbacks,1)
        self.assertEqual(self.conn.commits,0)
    def test_mysql_upsert_preserves_ingested_at_and_full_text(self):
        self.rss()
        run.extract(self.source)
        doc,images=next(adapters.iter_source(self.source))
        doc['content']='全长正文'*10000
        self.repo.save_article(doc,images)
        self.repo.save_article(doc,images)
        inserts=[(sql,args) for sql,args in self.conn.calls if sql.startswith('INSERT INTO `doc`')]
        self.assertEqual(len(inserts),2)
        for sql,args in inserts:
            self.assertNotIn('`ingested_at`',sql.split('ON DUPLICATE KEY UPDATE')[1])
            self.assertEqual(args[7],doc['content'])
            self.assertEqual(json.loads(args[3]),['研究员'])
        self.assertTrue(any('DELETE FROM `doc_image` WHERE `doc_id`=%s'==sql for sql,_ in self.conn.calls))
    def test_schema_validation_is_read_only_and_no_ddl(self):
        self.repo.validate()
        self.repo.start(self.source)
        self.repo.finish(42,self.source['key'],{},[])
        for sql,_ in self.conn.calls:
            self.assertNotRegex(sql.upper(),r'\b(CREATE|ALTER|DROP|TRUNCATE)\b')
        self.conn.missing='content'
        with self.assertRaisesRegex(ValueError,'content'): self.repo.validate()

class ServiceContracts(unittest.TestCase):
    def test_source_selection_and_all_29(self):
        self.assertEqual(len(adapters.sources()),29)
        self.assertEqual(run.select_sources(['openai'])[0]['key'],'web:openai')
        with self.assertRaises(ValueError): run.select_sources(['typo'])
    def test_s3_permission_errors_are_not_missing_objects(self):
        client=Mock()
        objects=ObjectStore(client=client)
        for code in ['404','NoSuchKey','NotFound']:
            client.head_object.side_effect=ClientError({'Error':{'Code':code}},'HeadObject')
            self.assertFalse(objects.exists('a'))
        client.head_object.side_effect=ClientError({'Error':{'Code':'403'}},'HeadObject')
        with self.assertRaises(ClientError): objects.exists('a')
    def test_s3_upload_must_be_verified(self):
        client=Mock()
        objects=ObjectStore(client=client)
        client.head_object.side_effect=ClientError({'Error':{'Code':'404'}},'HeadObject')
        with self.assertRaises(RuntimeError): objects.upload('a',PNG,'image/png')
        client.head_object.side_effect=None
        objects.upload('a',PNG,'image/png')
        self.assertEqual(client.put_object.call_count,2)
        client.create_bucket.assert_not_called()
    def test_internal_s3_does_not_inherit_website_proxy(self):
        env={'XSKY_ENDPOINT':'https://objects.test','XSKY_BUCKET':'images','XSKY_ACCESS_KEY':'a','XSKY_SECRET_KEY':'b','HTTP_PROXY':'http://public-proxy.test'}
        with patch.dict(os.environ,env), patch('boto3.client') as factory:
            ObjectStore()
        self.assertEqual(factory.call_args.kwargs['config'].proxies,{})
        self.assertEqual(factory.call_args.kwargs['endpoint_url'],'https://objects.test')
    def test_one_source_failure_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as td, patch.object(run,'STATE',Path(td)), \
             patch('run.setup_logging'), \
             patch('run.select_sources',return_value=[{'key':'web:bad','name':'bad'},{'key':'web:good','name':'good'}]), \
             patch('run.process',side_effect=[RuntimeError('bad'),True]) as process:
            self.assertEqual(run.main(['collect']),1)
            self.assertEqual(process.call_count,2)
    def test_mysql_required_tls_refuses_unencrypted_session(self):
        env={'MYSQL_HOST':'db.test','MYSQL_USER':'test','MYSQL_PASSWORD':'test',
             'MYSQL_DATABASE':'test','MYSQL_SSL_MODE':'REQUIRED'}
        connection=Mock()
        connection.cursor.return_value.__enter__=Mock(return_value=Mock())
        connection.cursor.return_value.__exit__=Mock(return_value=False)
        cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('Ssl_cipher','')
        with patch.dict(os.environ,env,clear=True),patch('pymysql.connect',return_value=connection):
            with self.assertRaisesRegex(ValueError,'TLS'):
                kb.connect()
        connection.close.assert_called_once()

    def test_mysql_required_tls_connects_when_encrypted(self):
        env={'MYSQL_HOST':'db.test','MYSQL_USER':'test','MYSQL_PASSWORD':'test',
             'MYSQL_DATABASE':'test','MYSQL_SSL_MODE':'REQUIRED'}
        connection=Mock()
        connection.cursor.return_value.__enter__=Mock(return_value=Mock())
        connection.cursor.return_value.__exit__=Mock(return_value=False)
        cursor=connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value=('Ssl_cipher','TLS_AES_256_GCM_SHA384')
        with patch.dict(os.environ,env,clear=True),patch('pymysql.connect',return_value=connection) as factory:
            self.assertIs(kb.connect(),connection)
        self.assertIsNotNone(factory.call_args.kwargs.get('ssl'))
        connection.close.assert_not_called()

    def test_wp_failure_does_not_discard_successful_substack_counts(self):
        from collectors import semianalysis as semi
        args=argparse.Namespace(full=True,redo=False,limit=None)
        with patch.object(semi,'load_state',return_value={}),patch.object(semi,'save_state'), \
             patch.object(semi,'collect_substack',return_value={'n_index':5,'n_fetched':2,'n_failed':0,'last_post_date':'2026-01-01'}), \
             patch.object(semi,'collect_wp',side_effect=RuntimeError('wp unavailable')):
            counts=run.collect({'_seg':'semi'},args)
        self.assertEqual(counts['n_fetched'],2)
        self.assertEqual(counts['n_failed'],1)
        self.assertEqual(counts['error'],'wp: RuntimeError')

if __name__=='__main__': unittest.main()
