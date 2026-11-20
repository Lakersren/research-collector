#!/usr/bin/env python3
"""Article collection pipeline, shared by manual CLI and service workers."""
import bootstrap
# A process gets a fresh workspace for this run, before any collector captures RAW paths.
import os
import sys
if __name__ == '__main__' and len(sys.argv)>1 and sys.argv[1] in ('collect','export','ingest'):
    from datetime import datetime,timezone
    from pathlib import Path
    import re
    import uuid
    from aris.daily_policy import yesterday
    run_id=os.environ.setdefault('CORPUS_RUN_ID',datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')+'-'+uuid.uuid4().hex)
    if not re.fullmatch(r'\d{20}-[0-9a-f]{32}',run_id):
        raise ValueError('无效的 CORPUS_RUN_ID')
    os.environ.setdefault('CORPUS_TARGET_DATE',yesterday())
    import signal
    def terminated(signum,frame):raise KeyboardInterrupt('采集进程收到停止信号')
    signal.signal(signal.SIGTERM,terminated)
    workspace=Path(os.environ['ARIS_ROOT'])/'data/daily'/run_id
    os.environ['ARIS_RAW_ROOT']=str(workspace/'raw')
    os.environ['ARIS_SOURCE_STATE_ROOT']=str(workspace/'state')
import argparse
import fcntl
import logging
import sys
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from aris.paths import STATE
from aris.kb_adapters import sources
from aris.local_images import LocalImages
from aris.batches import publish, stamp, BATCHES
from aris.image_ingest import sync_images
from aris.observability import log_context, setup_logging

log = logging.getLogger('corpus.pipeline')

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
    seg=source['_seg']
    if seg=='substack':
        from collectors.substack_sites import collect_site
        return collect_site(source,full=args.full,redo=args.redo,limit_posts=args.limit)
    if seg=='rss':
        from collectors.web_rss_sites import collect_site
        return collect_site(source,redo=args.redo,limit_items=args.limit,redo_dead=args.redo)
    if seg=='standalone':
        from collectors.epoch_site import collect as epoch_collect
        return epoch_collect(sections=None,redo=args.redo,limit=args.limit,full=args.full)
    from collectors import semianalysis as semi
    st=semi.load_state()
    totals=dict(n_index=0,n_fetched=0,n_failed=0)
    for channel, func in [('substack',semi.collect_substack)]+([('wp',semi.collect_wp)] if args.full else []):
        try:
            result=func(full=args.full,redo=args.redo,limit_posts=args.limit,st=st)
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
    with log_context(stage='publish'):
        manifest = publish(source, counts, errors, started)
        log.info('batch_published batch_id=%s files=%s status=%s',
                 manifest.id, len(manifest.files), manifest.collection.status)
    args.last_manifest = manifest
    return not errors


def main(argv=None):
    parser = argparse.ArgumentParser(description='外网文章采集、本地缓存与同步批次发布，不连接内部数据库')
    parser.add_argument('command', choices=['collect', 'export', 'ingest', 'check', 'sources'])
    parser.add_argument('--source', action='append', help='来源 ID，可重复指定；默认全部 29 个')
    parser.add_argument('--full', action='store_true', help='扩大来源清单扫描范围（仍只保留昨天发布的文章）')
    parser.add_argument('--redo', action='store_true', help='重新获取已缓存文章')
    parser.add_argument('--limit', type=int, help='每个来源本轮最多采集文章数')
    parser.add_argument('--job-id', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--trigger', choices=['manual', 'api', 'schedule'], default='manual', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.getenv("CORPUS_TARGET_DATE") and args.command in ("export","ingest"):
        args.command="collect"  # Compatibility aliases reprocess yesterday, never historical snapshots.
    if args.limit is not None and args.limit < 1:
        parser.error('--limit 必须大于 0')
    setup_logging('pipeline')
    job_id = args.job_id or str(uuid.uuid4())
    started = time.monotonic()
    report = None
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
                if args.command == 'check':
                    BATCHES.mkdir(parents=True, exist_ok=True)
                    probe=BATCHES/'.write-check';probe.write_text('ok');probe.unlink()
                    log.info('local_config_check_passed sources=%s',len(rows))
                    return 0
                if os.getenv('CORPUS_RUN_ID'):
                    from aris.daily_runs import begin,cleanup
                    from aris.paths import DATA
                    report=begin(job_id,rows,DATA,run_id=os.environ['CORPUS_RUN_ID'],target_date=os.environ['CORPUS_TARGET_DATE'])
                    cleanup(DATA,STATE)
                good = True
                for source_index, source in enumerate(rows):
                    with log_context(source_id=source['key'], source_name=source['name']):
                        source_started=stamp()
                        args.last_manifest=None
                        try:
                            audit = nullcontext()
                            if report:
                                from aris.article_audit import capture
                                from aris.daily_runs import audit_path
                                audit = capture(audit_path(DATA, os.environ['CORPUS_RUN_ID'], source_index))
                            with audit:
                                if not process(source,args): good=False
                        except Exception:
                            log.exception('source_execution_failed')
                            good=False
                        finally:
                            if report:
                                from aris.daily_runs import update_source
                                update_source(report,source['key'],args.last_manifest,source_started)
                if report:
                    from aris.daily_runs import finish
                    finish(report)
                return 0 if good else 1
        except BaseException:
            if report:
                from aris.daily_runs import finish
                finish(report,interrupted=True)
            log.exception('pipeline_failed duration_seconds=%.3f', time.monotonic() - started)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
