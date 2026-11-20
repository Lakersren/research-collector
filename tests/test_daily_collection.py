"""Calendar boundaries, source filtering, bounded cleanup and terminal report recovery."""
from datetime import date,datetime,timezone
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from datetime import timedelta
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

REPO=Path(__file__).resolve().parents[1];A=REPO/'research-collector'
sys.path[:0]=[str(A),str(A/'lib'),str(REPO/'research-data-service'),str(REPO/'research-data-service/lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
os.environ.setdefault('ARIS_ROOT',str(A))
from aris.daily_policy import matches,yesterday
from aris import daily_runs,web_media
from corpus_contract import Collection,RunReport
from collectors import substack_sites as sub,web_rss_sites as rss,epoch_site as epoch,semianalysis as semi

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.raw=self.root/'raw';self.state=self.root/'state'
        for module,attr,value in ((web_media,'RAW',self.raw),(sub,'STATE',self.state),(rss,'STATE',self.state),(epoch,'STATE',self.state),(semi,'BASE',self.raw/'web/semianalysis')):
            p=patch.object(module,attr,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'CORPUS_TARGET_DATE':'2026-11-18'});p.start();self.addCleanup(p.stop)
    def test_real_cli_only_publishes_yesterday_and_writes_terminal_report(self):
        from aris.daily_policy import SHANGHAI
        target=datetime.now(SHANGHAI).date()-timedelta(days=1)
        start = datetime.combine(target, datetime.min.time(), SHANGHAI).astimezone(timezone.utc)
        dates = [start-timedelta(seconds=1), start, start+timedelta(days=1, seconds=-1),
                 start+timedelta(days=1)]
        entries = []
        for i, stamp in enumerate(dates):
            body = "" if i == 2 else f"<content:encoded><![CDATA[<p>Body {i}</p>]]></content:encoded>"
            entries.append(f'<item><guid>{i}</guid><title>Article {i}</title><link>http://127.0.0.1:PORT/{i}</link><pubDate>{stamp.isoformat()}</pubDate>{body}</item>')
        entries.append(f'<item><guid>4</guid><title>Article 4</title><link>http://127.0.0.1:PORT/4</link><pubDate>{start.isoformat()}</pubDate><content:encoded><![CDATA[<p>Body 4</p>]]></content:encoded></item>')
        feed=('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'+''.join(entries)+'</channel></rss>').encode()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/2':
                    self.send_error(404)
                    return
                self.send_response(200);self.send_header('Content-Type','application/xml');self.end_headers()
                self.wfile.write(feed.replace(b'PORT',str(self.server.server_port).encode()))
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            import yaml
            (self.root/'config').mkdir()
            (self.root/'config/web_sources.yaml').write_text(yaml.safe_dump({'rss':[dict(key='web:fixture',name='Fixture',feed=f'http://127.0.0.1:{server.server_port}/feed',full_in_feed=True)]}))
            (self.root/'data/raw').mkdir(parents=True);(self.root/'data/raw/history').write_text('old')
            env={k:v for k,v in os.environ.items() if k not in ('ARIS_RAW_ROOT','CORPUS_RUN_ID','CORPUS_TARGET_DATE')}
            env.update(ARIS_ROOT=str(self.root),ARIS_OVERSEAS_DIRECT='1');env.pop('ARIS_PROXY_URL',None)
            result=subprocess.run([sys.executable,str(A/'run.py'),'collect','--source','fixture'],env=env,cwd=self.root,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,1,result.stderr)
            reports=list((self.root/'data/runs').glob('*.json'));self.assertEqual(len(reports),1)
            report=json.loads(reports[0].read_text())
            self.assertEqual(report['target_date'],target.isoformat())
            self.assertEqual(report['status'],'failed')
            source=report['sources'][0]
            audit=source['articles']
            batch=self.root/'data/batches'/source['batch_id']
            articles=[json.loads(x) for x in (batch/'articles.jsonl').read_text().splitlines() if x.strip()]
            self.assertCountEqual([row['title'] for row in articles],['Article 1','Article 4'])
            self.assertCountEqual([row['title'] for row in audit['success']],['Article 1','Article 4'])
            self.assertTrue(all(row['publication_date_beijing']==target.isoformat() for row in audit['success']))
            self.assertEqual(source['collection']['n_failed'],1)
            self.assertEqual(audit['failed'][0]['title'],'Article 2')
            self.assertEqual(audit['failed'][0]['publication_date_beijing'],target.isoformat())
            self.assertIn('404',audit['failed'][0]['reason'])
            self.assertEqual([row['title'] for row in audit['skipped']],['Article 0','Article 3'])
            self.assertEqual([row['publication_date_beijing'] for row in audit['skipped']],
                             [(target-timedelta(days=1)).isoformat(),(target+timedelta(days=1)).isoformat()])
            completion = reports[0].with_suffix('.md').read_text()
            self.assertIn('| Fixture | 失败 | 2 | 1 | 2 |', completion)
            self.assertIn('全部已保存文章的北京时间发布日期与目标日期一致', completion)
            self.assertIn(f'| 失败 | {target} |', completion)
            self.assertIn(f'| 跳过 | {target + timedelta(days=1)} |', completion)
            self.assertIn('Article 2', completion)
            self.assertIn('404', completion)
            self.assertFalse((self.root/'data/raw').exists())
            self.assertEqual(len(list((self.root/'data/daily').glob('*/raw/web/fixture/items/*.json'))),2)
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_beijing_midnight_boundaries(self):
        self.assertTrue(matches('2026-11-17T16:00:00Z'))
        self.assertTrue(matches('2026-11-18T15:59:59Z'))
        self.assertFalse(matches('2026-11-18T16:00:00Z'))
        self.assertFalse(matches('2026-11-17T15:59:59Z'))
        self.assertFalse(matches(None));self.assertFalse(matches('garbage'))
        self.assertEqual(yesterday(datetime(2026,11,18,17,tzinfo=timezone.utc)),'2026-11-18')
    def test_substack_fetches_only_yesterday_even_with_full(self):
        posts=[dict(id=i,slug=str(i),post_date=f'2026-11-{day}T03:00:00Z') for i,day in enumerate((17,18,19),1)]
        with patch.object(sub,'archive',return_value=posts),patch.object(sub,'post',return_value=dict(body_html='text')) as fetch,patch.object(sub.time,'sleep'):
            result=sub.collect_site(dict(key='web:test',name='test',host='https://example.test'),full=True,redo=False,limit_posts=None)
        self.assertEqual(result['n_fetched'],1);fetch.assert_called_once_with('https://example.test','2')
    def test_rss_and_html_only_save_yesterday(self):
        items=[dict(guid=str(i),date=f'2026-11-{day}T03:00:00Z',title=str(i),link='https://example.test/'+str(i),body_html='<p>body</p>') for i,day in enumerate((17,18,19),1)]
        for kind in ('rss','html_list'):
            with self.subTest(kind=kind),patch.object(rss,'fetch_text',return_value='fixture'),patch.object(rss,'parse_feed',return_value=items),patch.object(rss,'parse_html_list',return_value=items),patch.object(rss.time,'sleep'):
                result=rss.collect_site(dict(key='web:'+kind,name=kind,feed='https://example.test',kind=kind,full_in_feed=True),redo=False,limit_items=None)
                self.assertEqual(result['n_fetched'],1)
                saved=list((self.raw/'web'/kind/'items').glob('*.json'))
                self.assertEqual(len(saved),1);self.assertEqual(json.loads(saved[0].read_text())['title'],'2')
    def test_missing_date_is_not_saved_or_reported_as_success(self):
        item=dict(guid='one',date='',title='unknown date',link='https://example.test/one',body_html='<p>body</p>')
        with patch.object(rss,'fetch_text',return_value='fixture'),patch.object(rss,'parse_feed',return_value=[item]),patch.object(rss,'extract_article',return_value=dict(md='body',meta_title=None,meta_author=None,meta_date=None,meta_sitename=None)):
            result=rss.collect_site(dict(key='web:test',name='test',feed='https://example.test',full_in_feed=True),redo=False,limit_items=None)
        self.assertEqual(result['n_fetched'],0);self.assertEqual(result['n_failed'],1)
    def test_updates_do_not_substitute_for_publication(self):
        from aris.article_audit import page_publication_date
        feed = '<entry><title>Old article</title><link href="https://example.test/old"/><updated>2026-11-18T03:00:00Z</updated></entry>'
        self.assertEqual(rss.parse_feed(feed)[0]['date'], '')
        self.assertIsNone(page_publication_date('<meta property="article:modified_time" content="2026-11-18T03:00:00Z">'))
        self.assertEqual(page_publication_date('<meta property="article:published_time" content="2026-11-17T19:00:00-05:00">'),
                         '2026-11-17T19:00:00-05:00')

    def test_epoch_filters_dates_before_fetch_and_ignores_old_seen_checkpoint(self):
        items=[dict(iid=str(i),date=f'2026-11-{day}T03:00:00Z',title=str(i),url='https://example.test/'+str(i),section='publications',authors=[]) for i,day in enumerate((17,18,19),1)]
        ext=dict(md='body',meta_title='',meta_author='',meta_date='',meta_sitename='')
        with patch.object(epoch,'build_index',return_value=items),patch.object(epoch,'load_state',return_value={'seen':{'2':'old'}}),patch.object(epoch,'fetch_text',return_value='page') as fetch,patch.object(epoch,'extract_article',return_value=ext),patch.object(epoch.time,'sleep'):
            result=epoch.collect(sections=None,redo=False,limit=None,full=False,skip_crosscheck=True)
        self.assertEqual(result['n_fetched'],1);fetch.assert_called_once_with('https://example.test/2')
    def test_semianalysis_only_fetches_yesterday(self):
        posts=[dict(id=i,slug=str(i),post_date=f'2026-11-{day}T03:00:00Z') for i,day in enumerate((17,18,19),1)]
        with patch.object(semi,'sub_archive',return_value=posts),patch.object(semi,'sub_post',return_value={}) as fetch,patch.object(semi.time,'sleep'):
            result=semi.collect_substack(full=False,redo=False,limit_posts=None,st={})
        self.assertEqual(result['n_fetched'],1);fetch.assert_called_once_with('2')
    def test_retention_removes_legacy_cache_and_exact_seven_day_boundary(self):
        data=self.root/'data'
        for folder in ('raw','batches'):
            (data/folder).mkdir(parents=True);(data/folder/'old-history').write_text('old')
        daily_runs.cleanup(data,self.state,today=date(2026,11,19))
        self.assertFalse((data/'raw').exists());self.assertFalse((data/'batches').exists())
        for day in (12,13):
            # 03:00 UTC is the same calendar day in Shanghai.
            ident=f'202611{day}030000000000-'+'a'*32
            for folder in ('daily','batches'):
                (data/folder/ident).mkdir(parents=True)
            (data/'runs').mkdir(exist_ok=True);(data/'runs'/(ident+'.json')).write_text('{}')
            (data/'runs'/(ident+'.md')).write_text('# 采集任务完成表')
        daily_runs.cleanup(data,self.state,today=date(2026,11,19))
        for folder in ('daily','batches','runs'):
            kept = list((data/folder).iterdir())
            self.assertEqual(len(kept), 2 if folder == 'runs' else 1)
            self.assertTrue(all('20261113' in entry.name for entry in kept))
        self.assertTrue((self.state/'daily-only-v1.json').is_file())
    def test_report_recovery_marks_unfinished_sources_failed(self):
        sources=[dict(key='web:one',name='one'),dict(key='web:two',name='two')]
        job=str(uuid.uuid4());path=daily_runs.begin(job,sources,self.root,target_date='2026-11-18')
        report=daily_runs.finish(path,interrupted=True)
        self.assertEqual(report.status,'interrupted');self.assertEqual(len(report.sources),2)
        self.assertTrue(all(s.collection.status=='failed' and s.batch_id is None for s in report.sources))
        self.assertFalse(path.exists())
        saved=json.loads((self.root/'runs'/(report.id+'.json')).read_text())
        for source in saved['sources']:
            self.assertEqual(source.pop('articles'),{'success': [], 'failed': [], 'skipped': []})
        self.assertEqual(RunReport.model_validate(saved),report)

if __name__=='__main__':unittest.main()
