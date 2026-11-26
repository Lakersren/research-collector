"""Calendar boundaries, source filtering, bounded cleanup and terminal report recovery."""
from datetime import date,datetime,timezone
import gzip
from io import BytesIO
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
from aris import daily_archive,web_media
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
            env={k:v for k,v in os.environ.items() if k not in ('ARIS_RAW_ROOT','CORPUS_RUN_ID','CORPUS_TARGET_DATE')}
            env.update(ARIS_ROOT=str(self.root),ARIS_OVERSEAS_DIRECT='1');env.pop('ARIS_PROXY_URL',None)
            result=subprocess.run([sys.executable,str(A/'run.py'),'collect','--source','fixture'],env=env,cwd=self.root,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,3,result.stderr)
            archive = self.root / 'data/archive' / target.strftime('%Y%m%d')
            report=json.loads((archive / 'report.json').read_text())
            self.assertEqual(report['target_date'],target.isoformat())
            self.assertEqual(report['status'],'partial_success')
            source=report['sources'][0]
            audit=source['articles']
            self.assertCountEqual([row['title'] for row in audit['success']],['Article 1','Article 4'])
            self.assertTrue(all(row['publication_date_beijing']==target.isoformat() for row in audit['success']))
            self.assertEqual(source['collection']['n_failed'],1)
            self.assertEqual(audit['failed'][0]['title'],'Article 2')
            self.assertEqual(audit['failed'][0]['publication_date_beijing'],target.isoformat())
            self.assertIn('404',audit['failed'][0]['reason'])
            self.assertEqual([row['title'] for row in audit['skipped']],['Article 0','Article 3'])
            self.assertEqual([row['publication_date_beijing'] for row in audit['skipped']],
                             [(target-timedelta(days=1)).isoformat(),(target+timedelta(days=1)).isoformat()])
            completion = (archive / 'report.md').read_text()
            self.assertIn('| Fixture | 失败 | 2 | 1 | 2 |', completion)
            self.assertIn('全部已保存文章的北京时间发布日期与目标日期一致', completion)
            self.assertIn(f'| 失败 | {target} |', completion)
            self.assertIn(f'| 跳过 | {target + timedelta(days=1)} |', completion)
            self.assertIn('Article 2', completion)
            self.assertIn('404', completion)
            self.assertEqual(source['article_counts'], {'success': 2, 'failed': 1, 'skipped': 2})
            self.assertEqual(sorted((archive / item['text_path']).read_text() for item in audit['success']),
                             ['Body 1', 'Body 4'])
            self.assertEqual([path.name for path in (self.root/'data').iterdir()], ['archive'])
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_beijing_midnight_boundaries(self):
        self.assertTrue(matches('2026-11-17T16:00:00Z'))
        self.assertTrue(matches('2026-11-18T15:59:59Z'))
        self.assertFalse(matches('2026-11-18T16:00:00Z'))
        self.assertFalse(matches('2026-11-17T15:59:59Z'))
        self.assertFalse(matches(None));self.assertFalse(matches('garbage'))
        self.assertEqual(yesterday(datetime(2026,11,18,17,tzinfo=timezone.utc)),'2026-11-18')

    def test_rss_limit_applies_before_filtering_for_yesterday(self):
        import run

        feed = '<rss>' + ''.join(
            f'<item><title>Article {i}</title><link>https://example.test/{i}</link>'
            f'<pubDate>2026-11-{18 if i == 11 else 17}T00:00:00Z</pubDate></item>'
            for i in range(1, 12)
        ) + '</rss>'
        source = dict(key='web:test', name='Test', feed='https://example.test/feed', _seg='rss')
        from argparse import Namespace
        with patch.object(rss, 'fetch_text', return_value=feed) as fetch:
            result = run.collect(source, Namespace(limit=None, full=False, redo=False))
        self.assertEqual((result['n_index'], result['n_fetched'], result['n_failed']), (10, 0, 0))
        fetch.assert_called_once()

    def test_substack_stops_pagination_when_the_candidate_window_is_full(self):
        from aris.substack_api import archive
        from unittest.mock import Mock

        posts = [dict(id=i, post_date='2026-11-18T00:00:00Z') for i in range(1, 13)]
        fetch = Mock(return_value={'posts': posts, 'nextCursor': 'page-two'})
        self.assertEqual(archive(fetch, 'https://example.test', max_posts=10), posts[:10])
        fetch.assert_called_once()

    def test_sources_run_in_parallel_across_hosts_and_in_order_within_a_host(self):
        from argparse import Namespace
        from aris.archive_source import source_result, stamp
        import run

        sources = [
            dict(key='web:first', name='First', feed='https://one.test/a'),
            dict(key='web:second', name='Second', feed='https://www.one.test/b'),
            dict(key='web:other', name='Other', host='https://two.test'),
        ]
        first_finished = threading.Event()
        other_started = threading.Event()
        work = self.root / 'staging'
        work.mkdir()
        args = Namespace(limit=None, staging=work, target_date='2026-11-18')

        def process(source, options):
            key = source['key'].split(':')[-1]
            if key == 'first':
                self.assertTrue(other_started.wait(3))
                first_finished.set()
            elif key == 'other':
                other_started.set()
            else:
                self.assertTrue(first_finished.is_set())
            options.result = source_result(source, key, stamp())

        with (
            patch.object(run, 'process', side_effect=process),
            patch.object(run, 'publish_date') as publish,
            patch.object(run, 'prune_dates'),
        ):
            self.assertEqual(run.collect_day(sources, args, str(uuid.uuid4()), work), 0)
        report = publish.call_args.args[2]
        self.assertEqual([item['source_id'] for item in report['sources']],
                         [item['key'] for item in sources])

    def test_feed_decompresses_gzip_with_or_without_header(self):
        feed = b'<rss><channel><item><title>Article</title></item></channel></rss>'
        for headers in ({}, {'Content-Encoding': 'gzip'}):
            with self.subTest(headers=headers):
                response = BytesIO(gzip.compress(feed))
                response.headers = headers
                with patch.object(rss, 'overseas_urlopen', return_value=response):
                    self.assertEqual(rss.parse_feed(rss.fetch_text('https://example.test/feed'))[0]['title'],
                                     'Article')

    def test_anthropic_channels_use_their_own_article_links(self):
        sites = {site['key']: site for site in rss.load_cfg()}
        samples = {
            'web:anthropic': (
                '<main><a href="/news/policy/">Policy</a>'
                '<a href="/news/anthropic-prices-initial-public-offering">Anthropic prices initial public offering</a>'
                '</main>',
                'https://www.anthropic.com/news/anthropic-prices-initial-public-offering',
            ),
            'web:anthropic-engineering': (
                '<main><article><h3><a href="/research/components-of-agent-memory">'
                'The components of agent memory</a></h3></article>'
                '<a href="/research/team/alignment">Alignment</a></main>',
                'https://www.anthropic.com/research/components-of-agent-memory',
            ),
        }
        extracted = dict(md='body', meta_title=None, meta_author=None,
                         meta_date='2026-11-18T00:00:00Z', meta_sitename=None)
        for key, (listing, article_url) in samples.items():
            with (
                self.subTest(key=key),
                patch.object(rss, 'fetch_text', side_effect=[listing, '<html></html>']) as fetch,
                patch.object(rss, 'extract_article', return_value=extracted),
                patch.object(rss.time, 'sleep'),
            ):
                result = rss.collect_site(sites[key], redo=False, limit_items=None)
            self.assertEqual((result['n_index'], result['n_fetched'], result['n_failed']), (1, 1, 0))
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [sites[key]['feed'], article_url])

    def test_kimi_card_dates_filter_before_fetching_article_pages(self):
        def card(slug, title, published):
            return (f'<div class="menu-card"><a href="/en/blog/{slug}"></a>'
                    f'<div class="card-body"><h4 class="card-title">{title}</h4>'
                    f'<p class="card-date">{published}</p></div></div>')

        current = card('current', 'Current article', '2026-11-18')
        listing = '<main>' + card('older', 'Older article', '2026-11-17') + current * 2 + '</main>'
        config = dict(key='web:kimi', name='Kimi Blog', feed='https://www.kimi.com/blog/', kind='html_list')
        extracted = dict(md='body', meta_title=None, meta_author=None, meta_date=None, meta_sitename=None)
        with patch.object(rss, 'fetch_text', side_effect=[listing, '<html></html>']) as fetch, \
             patch.object(rss, 'extract_article', return_value=extracted), patch.object(rss.time, 'sleep'):
            result = rss.collect_site(config, redo=False, limit_items=None)
        self.assertEqual((result['n_index'], result['n_fetched'], result['n_failed']), (2, 1, 0))
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         [config['feed'], 'https://www.kimi.com/en/blog/current'])
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
    def test_retention_keeps_seven_target_dates(self):
        data = self.root / 'data'
        for day in ('20261111', '20261112', '20261118'):
            (data / 'archive' / day).mkdir(parents=True)
        daily_archive.prune_dates(data, today=date(2026, 11, 19))
        self.assertEqual(sorted(p.name for p in (data / 'archive').iterdir()), ['20261112', '20261118'])

if __name__=='__main__':unittest.main()
