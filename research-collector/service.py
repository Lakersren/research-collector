#!/usr/bin/env python3
"""In-process scheduler and authenticated manual HTTP triggers."""
import bootstrap
import argparse
from contextlib import AsyncExitStack, asynccontextmanager
import fcntl
import logging
import os
from pathlib import Path
import secrets
import sys
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
import yaml

from aris.dispatch import BusyError, Dispatcher
from aris.observability import setup_logging
from aris.paths import CONFIG, STATE
from run import select_sources

log = logging.getLogger('corpus.service')


def load_schedule(path):
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or set(raw) - {'timezone', 'jobs'}:
        raise ValueError('调度配置只允许 timezone 和 jobs')
    jobs = raw.get('jobs')
    if not isinstance(jobs, list):
        raise ValueError('jobs 必须是列表')
    timezone = raw.get('timezone', 'Asia/Shanghai')
    seen = set()
    result = []
    for row in jobs:
        item = ScheduleJob.model_validate(row)
        if item.id in seen:
            raise ValueError('调度任务 ID 重复')
        seen.add(item.id)
        select_sources(item.sources)
        trigger = CronTrigger.from_crontab(item.cron, timezone=timezone)
        result.append((item, trigger))
    return result


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    command: Literal['collect', 'export', 'ingest'] = 'collect'
    sources: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)
    full: bool = False
    redo: bool = False
    timeout_seconds: int = Field(default=7200, ge=1, le=86400)


class ScheduleJob(JobRequest):
    id: str = Field(min_length=1)
    enabled: bool = True
    cron: str


class AllSourcesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['all']


class SingleSourceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['source']
    source_id: str = Field(min_length=1)


TodayJobRequest = Annotated[AllSourcesRequest | SingleSourceRequest, Field(discriminator='scope')]


def scheduled_submit(dispatcher, item):
    try:
        result = dispatcher.submit(**item.model_dump(exclude={'id', 'enabled', 'cron'}), trigger='schedule')
        log.info('schedule_submitted schedule_id=%s job_id=%s', item.id, result['id'])
    except BusyError:
        log.warning('schedule_skipped_busy schedule_id=%s; next tick will retry', item.id)
    except Exception:
        log.exception('schedule_submission_failed schedule_id=%s', item.id)


def create_app(config_path=None, dispatcher_factory=Dispatcher, *, archive_root=None):
    config_path = config_path or CONFIG / 'schedule.yaml'
    token = os.getenv('CORPUS_API_TOKEN', '')
    if len(token) < 24:
        raise ValueError('请设置至少 24 个字符的 CORPUS_API_TOKEN')
    jobs = load_schedule(config_path)
    security = HTTPBearer(auto_error=False)

    def authenticated(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(status_code=401, detail='需要有效的 Bearer token',
                                headers={'WWW-Authenticate': 'Bearer'})

    @asynccontextmanager
    async def lifespan(app):
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'service.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('同一目录只能运行一个调度服务') from None
            async with AsyncExitStack() as stack:
                dispatcher = dispatcher_factory()
                stack.callback(dispatcher.shutdown)
                app.state.dispatcher = dispatcher
                scheduler = BackgroundScheduler()
                for item, trigger in jobs:
                    if item.enabled:
                        scheduler.add_job(scheduled_submit, trigger, id=item.id, args=[dispatcher, item],
                                          coalesce=True, max_instances=1, misfire_grace_time=60)
                app.state.scheduler = scheduler
                scheduler.start()
                stack.callback(scheduler.shutdown, wait=True)
                for job in scheduler.get_jobs():
                    log.info('schedule_registered id=%s next_run=%s', job.id, job.next_run_time)
                try:
                    yield
                finally:
                    log.info('service_stopping')

    app = FastAPI(title='Overseas Corpus', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'busy': app.state.dispatcher.active is not None}

    @app.get('/sources', dependencies=[Depends(authenticated)])
    def list_sources():
        return [{'id': s['key'], 'name': s['name']} for s in select_sources(None)]

    @app.get('/schedules', dependencies=[Depends(authenticated)])
    def schedules():
        return [{'id': job.id, 'next_run_at': job.next_run_time.isoformat() if job.next_run_time else None}
                for job in app.state.scheduler.get_jobs()]

    def submit_job(**options):
        try:
            return app.state.dispatcher.submit(**options, trigger='api')
        except BusyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None

    @app.post('/jobs', status_code=202, dependencies=[Depends(authenticated)])
    def submit(request: JobRequest):
        return submit_job(**request.model_dump())

    @app.post('/jobs/today', status_code=202, dependencies=[Depends(authenticated)])
    def submit_today(request: TodayJobRequest):
        sources = [request.source_id] if isinstance(request, SingleSourceRequest) else []
        return submit_job(command='collect', sources=sources)

    @app.get('/jobs', dependencies=[Depends(authenticated)])
    def recent():
        return app.state.dispatcher.recent()

    @app.get('/jobs/{job_id}', dependencies=[Depends(authenticated)])
    def job(job_id: str):
        try:
            return app.state.dispatcher.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail='任务不存在') from None

    from aris.transfer_api import install_archive_routes
    install_archive_routes(app, root=archive_root)
    return app


def main():
    parser = argparse.ArgumentParser(description='定时采集与手动 HTTP 触发服务')
    parser.add_argument('--config', type=Path, default=CONFIG / 'schedule.yaml')
    parser.add_argument('--host', default=os.getenv('CORPUS_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('CORPUS_PORT', '8000')))
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    logger = setup_logging('service')
    for name in ('uvicorn', 'apscheduler'):
        external = logging.getLogger(name)
        external.handlers = logger.handlers[:]
        external.propagate = False
        external.setLevel(logging.INFO)
    if args.check_config:
        for item, trigger in load_schedule(args.config):
            print(f'{item.id}: enabled={item.enabled}, {trigger}')
        return
    import uvicorn
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, workers=1,
                access_log=False, log_config=None, log_level='info')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log.exception('service_start_failed')
        raise SystemExit(1)
