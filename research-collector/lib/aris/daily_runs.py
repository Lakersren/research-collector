"""Atomic daily execution reports and bounded, collector-only disk retention."""
from datetime import datetime,timedelta,timezone
import json
import logging
from pathlib import Path
import re
import shutil
import uuid
from corpus_contract import RunReport
from aris.article_audit import article_details
from aris.completion_report import render
from aris.daily_policy import SHANGHAI,yesterday

ID=re.compile(r'^\d{20}-[0-9a-f]{32}$')
log=logging.getLogger('corpus.daily')

def now():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def new_id():return datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')+'-'+uuid.uuid4().hex

def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(value, encoding='utf-8')
    temp.replace(path)


def atomic(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))

def begin(job_id,sources,data,run_id=None,target_date=None):
    path=Path(data)/'runs'/('.pending-'+str(uuid.UUID(job_id))+'.json')
    if path.exists():return path
    atomic(path,dict(version=1,id=run_id or new_id(),job_id=job_id,target_date=target_date or yesterday(),
        timezone='Asia/Shanghai',started_at=now(),finished_at=None,status='running',
        sources=[dict(source_id=s['key'],source_name=s['name'],batch_id=None,collection=None) for s in sources]))
    return path

def audit_path(data, run_id, source_index):
    return Path(data) / 'daily' / run_id / f'article-audit-{source_index}.jsonl'


def article_results(data, run_id, source_index, source):
    results = {'success': [], 'failed': [], 'skipped': []}
    fetched = []
    journal = audit_path(data, run_id, source_index)
    if journal.is_file():
        with journal.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.endswith('\n'):
                    break  # Interrupted during the final write.
                row = json.loads(line)
                status = row.pop('status')
                if status == 'fetched':
                    fetched.append(row)
                else:
                    results[status].append(row)
    if source.batch_id:
        batch = Path(data) / 'batches' / source.batch_id / 'articles.jsonl'
        with batch.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    article = json.loads(line)
                    row = article_details(article['title'], article['url'], article['published_at'])
                    row['content_state'] = article['content_state']
                    row['images_failed'] = sum(image['status'] == 'failed' for image in article['images'])
                    results['success'].append(row)
    else:
        for row in fetched:
            row['reason'] = '文章已抓取，本轮批次未生成'
            results['failed'].append(row)
    return results


def update_source(path,source_id,manifest,started_at):
    value=json.loads(Path(path).read_text())
    entry=next(x for x in value['sources'] if x['source_id']==source_id)
    if manifest:
        entry.update(batch_id=manifest.id,collection=manifest.collection.model_dump())
    else:
        entry['collection']=dict(started_at=started_at,finished_at=now(),n_discovered=0,n_fetched=0,n_failed=1,
            status='failed',error='来源未能生成批次，请按 job_id 查询 pipeline.log')
    atomic(path,value)

def finish(path,interrupted=False):
    path=Path(path)
    if not path.exists():return None
    value=json.loads(path.read_text());finished=now()
    for entry in value['sources']:
        if entry['collection'] is None:
            entry['collection']=dict(started_at=value['started_at'],finished_at=finished,
                n_discovered=0,n_fetched=0,n_failed=1,status='failed',error='任务中断或执行异常，该来源未完成')
    value['finished_at']=finished
    value['status']='interrupted' if interrupted else ('failed' if any(x['collection']['status']=='failed' for x in value['sources']) else 'success')
    report=RunReport.model_validate(value)
    local_report = report.model_dump()
    for i, source in enumerate(report.sources):
        local_report['sources'][i]['articles'] = article_results(path.parent.parent, report.id, i, source)
    atomic_text(path.parent / (report.id + '.md'), render(local_report))
    atomic(path.parent/(report.id+'.json'),local_report)
    path.unlink(missing_ok=True)
    log.info('daily_report_finished run_id=%s target_date=%s status=%s',report.id,report.target_date,report.status)
    return report

def finish_job(job_id,data,interrupted=True):
    return finish(Path(data)/'runs'/('.pending-'+str(uuid.UUID(job_id))+'.json'),interrupted)

def remove(path):
    if path.is_symlink() or path.is_file():path.unlink()
    elif path.is_dir():shutil.rmtree(path)

def cleanup(data,state,today=None):
    """Keep execution dates today..today-6, including unacknowledged batches by policy."""
    data=Path(data);state=Path(state);state.mkdir(parents=True,exist_ok=True)
    marker=state/'daily-only-v1.json'
    if not marker.exists():
        # One-time migration explicitly discards old full-history caches and transfer snapshots.
        for path in (data/'raw',data/'batches'):
            if path.exists() or path.is_symlink():remove(path)
        atomic(marker,{'migrated_at':now(),'mode':'yesterday-only','retention_days':7})
        log.warning('legacy_history_cache_removed')
    cutoff=(today or datetime.now(SHANGHAI).date())-timedelta(days=7)
    removed=0
    for directory in (data / 'daily', data / 'batches', data / 'runs'):
        if directory.is_symlink():raise ValueError('retention root must not be a symlink')
        for path in directory.glob('*'):
            ident = path.name
            if directory.name == 'runs':
                if path.suffix not in {'.json', '.md', '.tmp'}:
                    continue
                ident = path.name.removesuffix('.tmp').removesuffix('.json').removesuffix('.md')
            if directory.name=='batches':ident=ident.lstrip('.')
            if not ID.fullmatch(ident):continue
            day=datetime.strptime(ident[:20],'%Y%m%d%H%M%S%f').replace(tzinfo=timezone.utc).astimezone(SHANGHAI).date()
            if day<=cutoff:
                remove(path);removed+=1
    for path in (data/'runs').glob('.pending-*'):
        if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime,SHANGHAI).date()<=cutoff:
            path.unlink();removed+=1
    for path in (state/'transfer-acks').glob('*.json'):
        if ID.fullmatch(path.stem) and not (data/'batches'/path.stem).is_dir():path.unlink()
    log.info('retention_finished retention_days=7 removed=%s',removed)
    return removed
