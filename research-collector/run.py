#!/usr/bin/env python3
"""Article collection pipeline, shared by manual CLI and service workers."""
import bootstrap
# A process gets a fresh workspace for this run, before any collector captures RAW paths.
import os
import sys
if __name__ == '__main__' and len(sys.argv)>1 and sys.argv[1] in ('collect','export','ingest'):
    from pathlib import Path
    from uuid import UUID, uuid4
    from aris.daily_policy import yesterday
    supplied_job_id = sys.argv[sys.argv.index('--job-id') + 1] if '--job-id' in sys.argv[:-1] else None
    job_id = UUID(os.environ.setdefault('CORPUS_JOB_ID', supplied_job_id or str(uuid4())))
    os.environ.setdefault('CORPUS_TARGET_DATE', yesterday())
    import signal
    def terminated(signum, frame):
        raise KeyboardInterrupt('采集进程收到停止信号')
    signal.signal(signal.SIGTERM, terminated)
    workspace = Path(os.environ['ARIS_ROOT']) / 'data' / f'.work-{job_id}'
    os.environ['ARIS_RAW_ROOT'] = str(workspace / 'raw')
    os.environ['ARIS_SOURCE_STATE_ROOT'] = str(workspace / 'state')
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from copy import copy
import fcntl
import logging
import time
import uuid
import shutil
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from aris.paths import DATA, STATE
from aris.kb_adapters import sources
from aris.local_images import LocalImages
from aris.article_audit import capture
from aris.archive_source import export_source, source_result, stamp
from aris.daily_archive import prune_dates, publish_date, source_directory
from aris.daily_policy import publication_day, normalize_target_date, yesterday
from aris.image_ingest import sync_images
from aris.observability import log_context, setup_logging

log = logging.getLogger('corpus.pipeline')
MAX_SOURCE_ARTICLES = 10
SOURCE_WORKERS = 3

def select_sources(names):
    rows=sources()
    if not names:
        return rows
    selected=[s for s in rows if s['key'] in names or s['key'].split(':',1)[-1] in names]
    unknown=set(names)-{v for s in selected for v in (s['key'],s['key'].split(':',1)[-1])}
    if unknown:
        raise ValueError('未知来源: '+', '.join(sorted(unknown)))
    return selected


def collect(source, args):
    limit = min(args.limit or MAX_SOURCE_ARTICLES, MAX_SOURCE_ARTICLES)
    seg=source['_seg']
    if seg=='substack':
        from collectors.substack_sites import collect_site
        return collect_site(source,full=args.full,redo=args.redo,limit_posts=limit)
    if seg=='rss':
        from collectors.web_rss_sites import collect_site
        return collect_site(source,redo=args.redo,limit_items=limit,redo_dead=args.redo)
    if seg=='standalone':
        from collectors.epoch_site import collect as epoch_collect
        return epoch_collect(sections=None,redo=args.redo,limit=limit,full=args.full,
                             skip_crosscheck=not args.full)
    from collectors import semianalysis as semi
    st=semi.load_state()
    totals=dict(n_index=0,n_fetched=0,n_failed=0)
    for channel, func in [('substack',semi.collect_substack)]+([('wp',semi.collect_wp)] if args.full else []):
        remaining = limit - totals['n_index']
        if remaining <= 0:
            break
        try:
            result=func(full=args.full,redo=args.redo,limit_posts=remaining,st=st)
            for k in totals:
                totals[k]+=result[k]
            if not result['n_failed']:
                st.setdefault(channel,{})['last_post_date']=result['last_post_date']
            semi.save_state(st)
        except Exception as e:
            totals['n_failed']+=1
            totals['error']=channel+': '+type(e).__name__
            log.exception('semianalysis_channel_failed channel=%s', channel)
    return totals


def extract(source):
    if source['_seg']=='substack':
        from collectors.substack_sites import extract_site
        return extract_site(source)
    if source['_seg']=='rss':
        from collectors.web_rss_sites import extract_site
        return extract_site(source)
    if source['_seg']=='standalone':
        from collectors.epoch_site import build_text
        return build_text()
    from collectors.semianalysis import extract_texts
    return extract_texts()



def process(source, args, objects=None):
    started = stamp()
    counts = {}
    errors = []
    if args.command == 'collect':
        try:
            counts = collect(source, args)
            if counts.get('n_failed') or counts.get('error') or counts.get('skipped'):
                errors.append('文章采集有失败，详见采集日志')
        except Exception:
            log.exception('article_collection_failed')
            errors.append('文章采集失败，详见采集日志')
    with log_context(stage='local_images'):
        failures = sync_images(source, objects or LocalImages())
        if failures: errors.append(f'{failures} 张图片获取失败')
    with log_context(stage='extract'):
        result = extract(source)
        if result.get('n_dirty'): raise ValueError('正文存在未清理 HTML，拒绝发布')
    with log_context(stage='archive'):
        folder = source_directory(source['key'])
        saved, files = export_source(source, args.staging, folder, args.target_date)
        args.result = source_result(source, folder, started, saved, files, counts, errors, args.audit)
    return not errors


def main(argv=None):
    parser = argparse.ArgumentParser(description='外网文章采集、按日归档，不连接内部数据库')
    parser.add_argument('command', choices=['collect', 'export', 'ingest', 'check', 'sources'])
    parser.add_argument('--source', action='append', help='来源 ID，可重复指定；默认全部 29 个')
    parser.add_argument('--full', action='store_true', help='扩大来源清单扫描范围（仍只保留昨天发布的文章）')
    parser.add_argument('--redo', action='store_true', help='重新获取已缓存文章')
    parser.add_argument('--limit', type=int, help='每个来源的候选文章数，默认 10，最大 10')
    parser.add_argument('--job-id', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--trigger', choices=['manual', 'api', 'schedule'], default='manual', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.getenv("CORPUS_TARGET_DATE") and args.command in ("export","ingest"):
        args.command="collect"  # Compatibility aliases reprocess yesterday, never historical snapshots.
    if args.limit is not None and args.limit < 1:
        parser.error('--limit 必须大于 0')
    setup_logging('pipeline')
    job_id = str(uuid.UUID(args.job_id or os.environ.get('CORPUS_JOB_ID') or str(uuid.uuid4())))
    started = time.monotonic()
    with log_context(job_id=job_id, trigger=args.trigger):
        try:
            rows = select_sources(args.source)
            if args.command == 'sources':
                for source in rows:
                    print(source['key'] + ' ' + source['name'])
                return 0
            STATE.mkdir(parents=True, exist_ok=True)
            with (STATE / 'pipeline.lock').open('w') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    log.warning('pipeline_busy: 已有采集/入库任务在运行')
                    return 75
                log.info('pipeline_started command=%s sources=%s', args.command,
                         [s.get('key', s['name']) for s in rows])
                DATA.mkdir(parents=True, exist_ok=True)
                if args.command == 'check':
                    from tempfile import TemporaryFile
                    with TemporaryFile(dir=DATA):
                        pass
                    log.info('local_config_check_passed sources=%s', len(rows))
                    return 0
                if args.command in ('export', 'ingest'):
                    args.command = 'collect'
                args.target_date = normalize_target_date(os.getenv('CORPUS_TARGET_DATE', yesterday()))
                work = DATA / f'.work-{job_id}'
                for orphan in DATA.glob('.work-*'):
                    if orphan.is_dir() and not orphan.is_symlink():
                        shutil.rmtree(orphan)
                args.staging = work / 'result'
                args.staging.mkdir(parents=True)
                try:
                    return collect_day(rows, args, job_id, work)
                finally:
                    shutil.rmtree(work)
        except BaseException:
            log.exception('pipeline_failed duration_seconds=%.3f', time.monotonic() - started)
            return 1


def collect_host(entries, args, work, stopping):
    results = {}
    for index, source in entries:
        if stopping.is_set():
            break
        source_args = copy(args)
        folder = source_directory(source['key'])
        source_started = stamp()
        source_args.audit = work / f'audit-{index}.jsonl'
        source_args.result = None
        error = '来源执行失败，请查看采集日志'
        with log_context(source_id=source['key'], source_name=source['name']):
            try:
                with capture(source_args.audit):
                    process(source, source_args)
            except KeyboardInterrupt:
                stopping.set()
                error = '任务中断'
            except Exception:
                log.exception('source_execution_failed')
            if source_args.result is None:
                partial = source_args.staging / folder
                if partial.is_dir():
                    shutil.rmtree(partial)
                source_args.result = source_result(source, folder, source_started,
                                                   errors=[error], audit=source_args.audit)
            (source_args.staging / folder).mkdir(exist_ok=True)
            results[index] = source_args.result
    return results


def collect_day(rows, args, job_id, work):
    started_at = stamp()
    replace_all = args.limit is None and len(rows) == len(sources())
    by_host = {}
    for index, source in enumerate(rows):
        address = source.get('host') or source.get('feed') or ''
        host = urlsplit(address).hostname or source['key']
        by_host.setdefault(host.removeprefix('www.'), []).append((index, source))

    stopping = Event()
    completed = {}
    with ThreadPoolExecutor(max_workers=SOURCE_WORKERS) as pool:
        futures = []
        try:
            for entries in by_host.values():
                futures.append(pool.submit(copy_context().run, collect_host, entries, args, work, stopping))
            for future in as_completed(futures):
                completed.update(future.result())
        except KeyboardInterrupt:
            stopping.set()
            for future in futures:
                future.cancel()
            for future in futures:
                if not future.cancelled():
                    completed.update(future.result())

    results = []
    for index, source in enumerate(rows):
        if index in completed:
            results.append(completed[index])
            continue
        folder = source_directory(source['key'])
        (args.staging / folder).mkdir(exist_ok=True)
        results.append(source_result(source, folder, stamp(), errors=['任务中断，来源未执行']))
    status = 'success'
    if stopping.is_set():
        status = 'interrupted'
    elif any(result['collection']['status'] == 'failed' for result in results):
        status = 'failed'
    report = dict(version=1, job_id=job_id, target_date=args.target_date, timezone='Asia/Shanghai',
                  execution_date=publication_day(started_at).isoformat(), started_at=started_at,
                  finished_at=stamp(), status=status, sources=results)
    destination = publish_date(DATA, args.staging, report, replace_all)
    prune_dates(DATA)
    log.info('daily_archive_published path=%s status=%s', destination, status)
    return int(status != 'success')


if __name__ == '__main__':
    raise SystemExit(main())
