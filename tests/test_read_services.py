"""Exercise SQL pagination, read-only transactions, public schemas and real MCP transport offline."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'research-collector'
os.environ.setdefault('ARIS_ROOT',str(ROOT))
sys.path[:0]=[str(ROOT),str(ROOT/'lib'),str(REPO/'research-data-service'),str(REPO/'research-data-service/lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
from fastapi.testclient import TestClient
import httpx
from jsonschema import Draft202012Validator, RefResolver
import pymysql
import yaml
from aris.read_data import ArticleReader, CursorCodec, ImageSigner, ReadError, SearchQuery, read_session
from aris.read_api import create_read_app
from aris.mcp_bridge import MCPBridge
import service
import api_service

READ_TOKEN='read-token-for-contract-tests-32-characters'
ADMIN_TOKEN='admin-token-for-contract-tests-32-characters'
CURSOR_SECRET='cursor-secret-for-contract-tests-48-characters'
NOW=datetime(2026,2,1,tzinfo=timezone.utc)
SOURCE='web:servethehome'
SECOND='web:semiengineering'
THIRD='web:openai'

sqlite3.register_adapter(datetime,lambda d:d.isoformat(' '))
sqlite3.register_converter('DATETIME',lambda b:datetime.fromisoformat(b.decode()))


class SQLiteCursor:
    def __init__(self,connection):
        self.connection=connection
        self.cursor=connection.db.cursor()
    def __enter__(self): return self
    def __exit__(self,*args): self.cursor.close()
    def execute(self,sql,params=()):
        self.connection.statements.append((sql,params))
        if sql=='START TRANSACTION READ ONLY':
            self.cursor.execute('PRAGMA query_only=ON')
            self.cursor.execute('BEGIN')
            return
        sql=sql.replace('CAST(d.id AS BINARY)','CAST(d.id AS BLOB)').replace('CAST(%s AS BINARY)','CAST(%s AS BLOB)')
        sql=sql.replace("LEFT(TRIM(REGEXP_REPLACE(d.content,'[[:space:]]+',' ')),300)",
                        "substr(trim(regexp_replace(d.content,'[[:space:]]+',' ')),1,300)")
        self.cursor.execute(sql.replace('%s','?'),params)
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


class SQLiteConnection:
    def __init__(self,path,statements):
        self.statements=statements
        self.db=sqlite3.connect(path,detect_types=sqlite3.PARSE_DECLTYPES,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.create_collation('utf8mb4_0900_as_ci',lambda a,b:(a.casefold()>b.casefold())-(a.casefold()<b.casefold()))
        self.db.create_function('regexp_replace',3,lambda text,pattern,repl:re.sub(r'\s+',repl,text) if text is not None else None)
        self.closed=False
    def cursor(self,*args): return SQLiteCursor(self)
    def rollback(self): self.db.rollback()
    def close(self):
        self.db.close(); self.closed=True


class FakeSigner:
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def sign(self,key): return 'https://objects.test/'+key+'?signature=temporary','2026-02-01T00:15:00Z'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.dbpath=self.path/'fixture.sqlite'
        self.statements=[];self.connections=[]
        db=sqlite3.connect(self.dbpath)
        self.addCleanup(db.close)
        with db:
            db.executescript('''
                CREATE TABLE source(id TEXT PRIMARY KEY,n_docs INTEGER,first_ts DATETIME,last_ts DATETIME);
                CREATE TABLE doc(id TEXT PRIMARY KEY,source_id TEXT,title TEXT,authors TEXT,published_at DATETIME,
                                 ingested_at DATETIME,url TEXT,content_state TEXT,content TEXT);
                CREATE TABLE doc_image(doc_id TEXT,position INTEGER,file_key TEXT,status TEXT,width INTEGER,height INTEGER);
            ''')
            db.execute('INSERT INTO source VALUES(?,?,?,?)',(SOURCE,5,datetime(2026,1,1,9),datetime(2026,1,1,10)))
            for index,(name,title,state,published) in enumerate([
                ('A','NVIDIA 100%_GPU','ready',datetime(2026,1,1,10)),
                ('B','nvidia report','ready',datetime(2026,1,1,10)),
                ('C','AMD report','ready',datetime(2026,1,1,9)),
                ('D','Preview','preview',None),
                ('E','Only title','no_text',None),
            ]):
                db.execute('INSERT INTO doc VALUES(?,?,?,?,?,?,?,?,?)',
                           (SOURCE+':'+name,SOURCE,title,'["作者"]',published,datetime(2026,1,2,8,index),
                            'https://example.test/'+name,state,None if state=='no_text' else '  Full\n body [图1] [图2]  '))
            db.execute('INSERT INTO doc VALUES(?,?,?,?,?,?,?,?,?)',('hidden','hidden-source','private','[]',None,
                        datetime(2026,1,2),'https://example.test/private','ready','private body'))
            db.executemany('INSERT INTO doc_image VALUES(?,?,?,?,?,?)',[
                (SOURCE+':A',1,'overseas-corpus/images/a.png','ok',120,80),
                (SOURCE+':A',2,None,'failed',None,None)])
        def connection_factory():
            connection=SQLiteConnection(self.dbpath,self.statements)
            self.connections.append(connection)
            return connection
        self.sessions=lambda:read_session(connection_factory)
        self.reader=ArticleReader(CURSOR_SECRET,session_factory=self.sessions,
                                 source_factory=lambda:[{'key':x,'name':x} for x in (SOURCE,SECOND,THIRD)],
                                 signer_factory=FakeSigner,clock=lambda:NOW)
        self.app=create_read_app(READ_TOKEN,CURSOR_SECRET,self.reader)
        self.client=TestClient(self.app)
        self.headers={'Authorization':'Bearer '+READ_TOKEN}
    def get(self,path,**kwargs): return self.client.get(path,headers=self.headers,**kwargs)

    def assert_contract(self,name,response):
        api=yaml.safe_load((REPO/'contracts/openapi-read.yaml').read_text())
        validator=Draft202012Validator(api['components']['schemas'][name],resolver=RefResolver.from_schema(api),
                                        format_checker=Draft202012Validator.FORMAT_CHECKER)
        validator.validate(response.json())


class ReadAPITests(Fixture):
    def test_sources_include_configured_but_empty_and_hide_internals(self):
        response=self.get('/sources')
        self.assertEqual(response.status_code,200)
        self.assert_contract('SourcesResponse',response)
        rows=response.json()['data']['items']
        self.assertEqual(len(rows),3)
        empty=next(x for x in rows if x['id']==THIRD)
        self.assertEqual(empty['n_docs'],0)
        self.assertIsNone(empty['first_ts'])
        self.assertNotIn('note',empty)
        self.assertTrue(all(c.closed for c in self.connections))
        self.assertEqual(self.statements[0][0],'START TRANSACTION READ ONLY')

    def test_auth_headers_and_errors_are_stable(self):
        for token in ('wrong',ADMIN_TOKEN):
            response=self.client.get('/articles',headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,401)
            self.assert_contract('ErrorResponse',response)
        rid='7afba495-ff09-414d-bf7a-159093ac397e'
        response=self.client.get('/articles',headers={**self.headers,'X-Request-ID':rid})
        self.assertEqual(response.headers['X-Request-ID'],rid)
        self.assertEqual(response.json()['request_id'],rid)
        self.assertEqual(response.headers['Cache-Control'],'private, no-store')

    def test_sql_pagination_handles_equal_times_null_times_and_binary_id(self):
        ids=[];cursor=None
        for _ in range(3):
            params={'page_size':2}
            if cursor: params['cursor']=cursor
            response=self.get('/articles',params=params)
            self.assertEqual(response.status_code,200,response.text)
            self.assert_contract('ArticlesResponse',response)
            data=response.json()['data']
            ids.extend(row['id'].rsplit(':',1)[-1] for row in data['items'])
            cursor=data['page']['next_cursor']
        self.assertEqual(ids,['B','A','C','E','D'])
        self.assertIsNone(cursor)
        response=self.get('/articles',params={'sort':'ingested_at'})
        self.assertEqual([x['id'].rsplit(':',1)[-1] for x in response.json()['data']['items']],['E','D','C','B','A'])

    def test_literal_keyword_and_sql_injection_do_not_expand_search(self):
        for keyword,expected in [('nvidia',2),('%_',1),("' OR 1=1 --",0),('does-not-exist',0)]:
            response=self.get('/articles',params={'q':keyword})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(len(response.json()['data']['items']),expected)
        sql,params=next((sql,params) for sql,params in reversed(self.statements) if 'LIKE' in sql)
        self.assertNotIn('does-not-exist',sql)
        self.assertIn('%does-not-exist%',params)

    def test_both_time_windows_and_boundary_semantics(self):
        response=self.get('/articles',params={
            'published_from':'2026-01-01T18:00:00+08:00','published_to':'2026-01-01T11:00:00Z',
            'ingested_from':'2026-01-02T08:01:00Z','ingested_to':'2026-01-02T08:02:00Z'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([x['id'] for x in response.json()['data']['items']],[SOURCE+':B'])

    def test_invalid_queries_and_unknown_source_return_422(self):
        for params in [{'q':' '},{'q':'x'*201},{'unexpected':'value'},{'source_id':'typo'},
                       {'published_from':'2026-01-01'},{'published_from':'2026-01-01T00:00:00'},
                       {'published_from':'2026-01-02T00:00:00Z','published_to':'2026-01-01T00:00:00Z'},
                       {'page_size':101},{'page_size':'1.2'},{'content_state':'dirty_html'},
                       {'sort':'id; DROP TABLE doc'},[('q','a'),('q','b')],
                       [('source_id',SOURCE),('source_id',SOURCE)]]:
            response=self.get('/articles',params=params)
            self.assertEqual(response.status_code,422,str(params)+response.text)
            self.assert_contract('ErrorResponse',response)
        self.assertEqual(self.client.request('GET','/sources',headers=self.headers,json={'x':1}).status_code,422)

    def test_cursor_cannot_change_filters_page_size_or_signature(self):
        first=self.get('/articles',params={'page_size':1}).json()['data']['page']['next_cursor']
        for params in [{'page_size':2,'cursor':first},{'page_size':1,'cursor':first,'q':'nvidia'},
                       {'page_size':1,'cursor':first[:-1]+('A' if first[-1]!='A' else 'B')}]:
            self.assertEqual(self.get('/articles',params=params).status_code,422)
        self.reader.cursors.clock=lambda:NOW+timedelta(days=2)
        self.assertEqual(self.get('/articles',params={'page_size':1,'cursor':first}).status_code,422)

    def test_snapshot_excludes_newly_ingested_articles(self):
        first=self.get('/articles',params={'page_size':1}).json()['data']['page']['next_cursor']
        db=sqlite3.connect(self.dbpath)
        self.addCleanup(db.close)
        with db:
            db.execute('INSERT INTO doc VALUES(?,?,?,?,?,?,?,?,?)',(SOURCE+':NEW',SOURCE,'new','[]',datetime(2025,1,1),
                        (NOW+timedelta(seconds=1)).replace(tzinfo=None),'https://example.test/new','ready','new'))
        result=self.get('/articles',params={'page_size':1,'cursor':first})
        self.assertNotIn(SOURCE+':NEW',json.dumps(result.json()))

    def test_detail_and_images_preserve_states_and_hide_file_keys(self):
        response=self.get('/articles/'+SOURCE+':A')
        self.assertEqual(response.status_code,200,response.text)
        self.assert_contract('ArticleResponse',response)
        article=response.json()['data']
        self.assertEqual(article['content'],'  Full\n body [图1] [图2]  ')
        self.assertEqual([x['position'] for x in article['images']],[1,2])
        self.assertIsNone(article['images'][1]['url'])
        self.assertNotIn('file_key',article['images'][0])
        images=self.get('/articles/'+SOURCE+':A/images')
        self.assert_contract('ImagesResponse',images)
        self.assertEqual(images.json()['data']['items'],article['images'])
        self.assertEqual(self.get('/articles/'+SOURCE+':D').json()['data']['content_state'],'preview')
        no_text=self.get('/articles/'+SOURCE+':E').json()['data']
        self.assertEqual(no_text['content_state'],'no_text')
        self.assertIsNone(no_text['content'])
        self.assertEqual(self.get('/articles/'+SOURCE+':B/images').json()['data']['items'],[])
        for article_id in ['missing','hidden']:
            self.assertEqual(self.get('/articles/'+article_id).status_code,404)

    def test_dependency_failure_uses_public_error_not_raw_exception(self):
        @contextmanager
        def failed():
            raise pymysql.OperationalError(2003,'private connection details')
            yield
        self.reader.sessions=failed
        response=self.get('/sources')
        self.assertEqual(response.status_code,503)
        self.assert_contract('ErrorResponse',response)
        self.assertNotIn('private connection',response.text)

    def test_ok_image_missing_key_is_internal_error(self):
        db=sqlite3.connect(self.dbpath)
        self.addCleanup(db.close)
        with db:
            db.execute('UPDATE doc_image SET file_key=NULL WHERE position=1')
        response=self.get('/articles/'+SOURCE+':A')
        self.assertEqual(response.status_code,500)
        self.assertNotIn('file_key',response.text)


class MCPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,{
            'CORPUS_API_TOKEN':ADMIN_TOKEN,'CORPUS_READ_API_TOKEN':READ_TOKEN,'CORPUS_CURSOR_SECRET':CURSOR_SECRET,
            'CORPUS_MCP_ALLOWED_HOSTS':'testserver','CORPUS_MCP_ALLOWED_ORIGINS':'http://testserver'})
        self.env.start();self.addCleanup(self.env.stop)
        holder={}
        def client_factory():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=holder['app']),trust_env=False)
        self.root=api_service.create_app(reader=self.reader,
                                        api_base_url='http://testserver',mcp_client_factory=client_factory)
        holder['app']=self.root
        self.client=TestClient(self.root)
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)
        self.mcp_headers={**self.headers,'Accept':'application/json, text/event-stream'}

    def rpc(self,method,params=None,rpc_id=1,headers=None):
        return self.client.post('/mcp',headers=headers or self.mcp_headers,
                                json={'jsonrpc':'2.0','id':rpc_id,'method':method,'params':params or {}})

    def initialize(self):
        response=self.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},
                                       'clientInfo':{'name':'test','version':'1'}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('protocolVersion',response.json()['result'])

    def test_official_transport_lists_and_calls_all_four_tools(self):
        self.initialize()
        response=self.rpc('tools/list')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()['result']['tools']),4)
        for name,args in [('list_sources',{}),('search_articles',{'q':'NVIDIA','page_size':1}),
                          ('get_article',{'id':SOURCE+':A'}),('get_article_images',{'id':SOURCE+':A'})]:
            response=self.rpc('tools/call',{'name':name,'arguments':args})
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()['result']
            self.assertFalse(result.get('isError'),result)
            self.assertIn('data',result['structuredContent'])
            self.assertEqual(json.loads(result['content'][0]['text']),result['structuredContent'])

    def test_official_client_session_discovers_and_calls_tools(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.root),
                    headers=self.headers,trust_env=False) as http:
                async with streamable_http_client('http://testserver/mcp',http_client=http) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        initialized=await session.initialize()
                        self.assertEqual(initialized.serverInfo.name,'overseas-corpus')
                        tools=await session.list_tools()
                        self.assertEqual(len(tools.tools),4)
                        for name,args in [('list_sources',{}),('search_articles',{'page_size':1}),
                                          ('get_article',{'id':SOURCE+':A'}),
                                          ('get_article_images',{'id':SOURCE+':A'})]:
                            result=await session.call_tool(name,args)
                            self.assertFalse(result.isError,result)
                            self.assertIn('data',result.structuredContent)
                            self.assertEqual(json.loads(result.content[0].text),result.structuredContent)
        self.client.portal.call(run)

    def test_mcp_auth_and_admin_permissions_are_separate(self):
        self.assertEqual(self.client.post('/mcp',json={}).status_code,401)
        self.assertEqual(self.rpc('tools/list',headers={**self.mcp_headers,'Authorization':'Bearer '+ADMIN_TOKEN}).status_code,401)
        self.assertEqual(self.client.get('/jobs',headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/v1/sources',headers={'Authorization':'Bearer '+ADMIN_TOKEN}).status_code,401)
        self.assertEqual(self.client.get('/jobs',headers={'Authorization':'Bearer '+ADMIN_TOKEN}).status_code,404)

    def test_api_has_no_collection_routes_scheduler_or_lock(self):
        self.assertEqual(self.client.get('/health').json()['service'],'read-api-mcp')
        self.assertFalse(hasattr(self.root.state,'scheduler'))
        self.assertFalse(hasattr(self.root.state,'dispatcher'))
        for path in ('/jobs','/schedules','/sources'):
            self.assertEqual(self.client.get(path,headers=self.headers).status_code,404)
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={}).status_code,404)
        self.assertFalse((self.path/'service.lock').exists())

    def test_tool_errors_are_not_empty_success(self):
        for args in [{'id':'missing'},{'id':'hidden'}]:
            result=self.rpc('tools/call',{'name':'get_article','arguments':args}).json()['result']
            self.assertTrue(result['isError'])
            self.assertNotIn('structuredContent',result)
            self.assertEqual(json.loads(result['content'][0]['text'])['error']['code'],'NOT_FOUND')
        result=self.rpc('tools/call',{'name':'search_articles','arguments':{'q':'a','limit':2}}).json()['result']
        self.assertTrue(result['isError'])
        result=self.rpc('tools/call',{'name':'search_articles','arguments':{'source_id':['typo']}}).json()['result']
        self.assertTrue(result['isError'])
        self.assertEqual(json.loads(result['content'][0]['text'])['error']['code'],'INVALID_ARGUMENT')

    def test_mcp_sdk_rejects_untrusted_host_and_origin(self):
        response=self.rpc('tools/list',headers={**self.mcp_headers,'Host':'evil.example'})
        self.assertEqual(response.status_code,421)
        response=self.rpc('tools/list',headers={**self.mcp_headers,'Origin':'https://evil.example'})
        self.assertEqual(response.status_code,403)


class IsolatedBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_and_non_json_errors_are_sanitized(self):
        for failure in [True,False]:
            def handler(request):
                if failure: raise httpx.ConnectError('private endpoint detail',request=request)
                return httpx.Response(502,text='private gateway HTML')
            bridge=MCPBridge(READ_TOKEN,'http://api.test',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            result=await bridge.call('list_sources',{},READ_TOKEN,'7afba495-ff09-414d-bf7a-159093ac397e')
            self.assertTrue(result.isError)
            self.assertNotIn('private',result.content[0].text)
            self.assertEqual(json.loads(result.content[0].text)['error']['code'],'SERVICE_UNAVAILABLE')

    async def test_mcp_forwards_only_incoming_read_auth_and_request_id(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'data':{'items':[]},'request_id':'7afba495-ff09-414d-bf7a-159093ac397e'})
        bridge=MCPBridge(READ_TOKEN,'http://api.test',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        result=await bridge.call('list_sources',{},READ_TOKEN,'7afba495-ff09-414d-bf7a-159093ac397e')
        self.assertIsInstance(result,dict)
        self.assertEqual(seen[0].headers['authorization'],'Bearer '+READ_TOKEN)
        self.assertEqual(seen[0].url.path,'/v1/sources')
        self.assertEqual(seen[0].headers['x-request-id'],'7afba495-ff09-414d-bf7a-159093ac397e')


class DeploymentSeparationTests(unittest.TestCase):
    def test_collector_never_installs_read_services_even_with_read_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);config=path/'schedule.yaml';config.write_text('jobs: []\n')
            dispatcher=Mock();dispatcher.active=None
            with patch.dict(os.environ,{'CORPUS_API_TOKEN':ADMIN_TOKEN,'CORPUS_TRANSFER_TOKEN':'transfer-token-for-tests-32-chars',
                    'CORPUS_READ_API_TOKEN':READ_TOKEN,'CORPUS_CURSOR_SECRET':CURSOR_SECRET}), \
                    patch.object(service,'STATE',path):
                app=service.create_app(config,lambda:dispatcher)
                with TestClient(app) as client:
                    self.assertEqual(client.get('/health').status_code,200)
                    self.assertTrue(hasattr(app.state,'scheduler'))
                    for route in ('/v1/sources','/v1/articles','/mcp'):
                        self.assertEqual(client.get(route).status_code,404)

    def test_api_requires_read_configuration_without_management_token(self):
        with patch.dict(os.environ,{'CORPUS_READ_API_TOKEN':READ_TOKEN,
                'CORPUS_CURSOR_SECRET':CURSOR_SECRET},clear=True):
            app=api_service.create_app()
            self.assertIsNotNone(app)
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError): api_service.create_app()


class SigningAndCleanupTests(unittest.TestCase):
    def test_real_s3v4_signing_uses_public_endpoint_and_expiry(self):
        from urllib.parse import urlsplit,parse_qs
        with patch.dict(os.environ,{'XSKY_ENDPOINT':'http://private.test:9000',
                'CORPUS_IMAGE_PUBLIC_ENDPOINT':'https://images.test','XSKY_BUCKET':'alpha',
                'XSKY_ACCESS_KEY':'fake-access','XSKY_SECRET_KEY':'fake-secret',
                'XSKY_REGION':'us-east-1','XSKY_ADDRESSING_STYLE':'path'},clear=True):
            with ImageSigner() as signer:
                url,expires=signer.sign('overseas-corpus/images/测试.png')
                parsed=urlsplit(url);query=parse_qs(parsed.query)
                self.assertEqual(parsed.netloc,'images.test')
                self.assertTrue(parsed.path.startswith('/alpha/overseas-corpus/images/'))
                self.assertEqual(query['X-Amz-Expires'],['900'])
                self.assertEqual(query['X-Amz-Algorithm'],['AWS4-HMAC-SHA256'])
                signed=datetime.strptime(query['X-Amz-Date'][0],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
                self.assertEqual(datetime.fromisoformat(expires.replace('Z','+00:00')),signed+timedelta(seconds=900))

    def test_missing_or_invalid_signer_configuration_is_unavailable(self):
        for env in ({},{'XSKY_ENDPOINT':'invalid','XSKY_BUCKET':'alpha',
                       'XSKY_ACCESS_KEY':'fake','XSKY_SECRET_KEY':'fake'}):
            with patch.dict(os.environ,env,clear=True),ImageSigner() as signer:
                with self.assertRaises(ReadError) as raised: signer.sign('image.png')
                self.assertEqual(raised.exception.status,503)

    def test_read_transaction_closes_after_begin_or_query_failure(self):
        for fail_begin in (True,False):
            connection=Mock()
            connection.cursor.return_value=Mock(__enter__=Mock(),__exit__=Mock())
            if fail_begin:
                connection.cursor.return_value.__enter__=Mock(side_effect=RuntimeError('begin failed'))
            else:
                connection.cursor=Mock(return_value=Mock(__enter__=Mock(return_value=Mock()),__exit__=Mock()))
            with self.assertRaises(RuntimeError):
                with read_session(lambda:connection): raise RuntimeError('query failed')
            connection.rollback.assert_called_once()
            connection.close.assert_called_once()


if __name__=='__main__': unittest.main()
