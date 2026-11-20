"""One bounded subprocess at a time, shared by scheduler and HTTP triggers."""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from aris.observability import log_context
from aris.paths import ROOT, STATE

log = logging.getLogger('corpus.dispatch')


class BusyError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class Dispatcher:
    def __init__(self, state=None, root=None):
        self.directory = Path(state if state is not None else STATE) / 'jobs'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.root = Path(root if root is not None else ROOT)
        self.report_data = Path(state).parent/'data' if state is not None else self.root/'data'
        self.guard = threading.RLock()
        self.active = None
        self.thread = None
        self.process = None
        self.stopping = False
        # This class is constructed after the service singleton lock is acquired.
        for path in self.directory.glob('*.json'):
            record = json.loads(path.read_text())
            if record['status'] in ('queued', 'running'):
                record.update(status='interrupted', finished_at=now(), error='服务重启，前次任务未正常完成')
                self._save(record)
                from aris.daily_runs import finish_job
                finish_job(record['id'],self.report_data)
                log.warning('recovered_interrupted_job job_id=%s', record['id'])

    def _save(self, record):
        path = self.directory / (record['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def get(self, job_id):
        try:
            normalized = str(uuid.UUID(job_id))
        except (ValueError, AttributeError):
            raise KeyError(job_id) from None
        path = self.directory / (normalized + '.json')
        if not path.exists():
            raise KeyError(job_id)
        return json.loads(path.read_text(encoding='utf-8'))

    def recent(self):
        paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        return [json.loads(p.read_text(encoding='utf-8')) for p in paths[:50]]

    def submit(self, *, command='collect', sources=None, limit=None, full=False, redo=False,
               trigger='api', timeout_seconds=7200):
        # The same validation applies to HTTP and scheduled jobs, before accepting work.
        from run import select_sources
        selected = select_sources(sources)
        if command not in ('collect', 'export', 'ingest'):
            raise ValueError('只支持 collect / export')
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('limit 必须是正整数')
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ValueError('timeout_seconds 必须是正整数')
        with self.guard:
            if self.active or self.stopping:
                raise BusyError('已有任务执行中或服务正在停止，请稍后重试')
            job_id = str(uuid.uuid4())
            from aris.daily_runs import new_id
            from aris.daily_policy import yesterday
            record = dict(run_id=new_id(),target_date=yesterday(),id=job_id, command=command, sources=[s['key'] for s in selected],
                          limit=limit, full=full, redo=redo, trigger=trigger, status='queued',
                          created_at=now(), started_at=None, finished_at=None, exit_code=None,
                          error=None, timeout_seconds=timeout_seconds)
            self._save(record)
            self.active = job_id
            self.thread = threading.Thread(target=self._execute, args=(record,), daemon=False)
            try:
                self.thread.start()
            except Exception:
                self.active = None
                record.update(status='failed', finished_at=now(), error='无法启动执行线程')
                self._save(record)
                raise
            return dict(record)

    def _execute(self, record):
        with log_context(job_id=record['id'], trigger=record['trigger']):
            record.update(status='running', started_at=now())
            try:
                self._save(record)
                from aris.daily_runs import begin
                from run import select_sources
                begin(record['id'],select_sources(record['sources']),self.report_data,record['run_id'],record['target_date'])
                args = [sys.executable, '-u', str(self.root / 'run.py'), record['command'],
                        '--job-id', record['id'], '--trigger', record['trigger']]
                for source in record['sources']:
                    args.extend(['--source', source])
                if record['limit'] is not None:
                    args.extend(['--limit', str(record['limit'])])
                for flag in ('full', 'redo'):
                    if record[flag]:
                        args.append('--' + flag)
                log.info('job_started command=%s sources=%s', record['command'], record['sources'])
                with self.guard:
                    if self.stopping:
                        raise RuntimeError('服务停止，未启动任务')
                    # Structured worker logs already live in pipeline.log. Capture unexpected
                    # stderr as well, e.g. an import failure before worker logging initializes.
                    self.process = subprocess.Popen(args, cwd=self.root,
                                                    env={**os.environ,'CORPUS_RUN_ID':record['run_id'],'CORPUS_TARGET_DATE':record['target_date']},
                                                    stdout=subprocess.PIPE,
                                                    stderr=subprocess.STDOUT, text=True,
                                                    encoding='utf-8', errors='replace')
                    process = self.process
                reader = threading.Thread(target=self._capture_output,
                                          args=(process.stdout, record), daemon=True)
                reader.start()
                try:
                    code = process.wait(timeout=record['timeout_seconds'])
                    record.update(exit_code=code, status='success' if code == 0 else 'failed',
                                  error=None if code == 0 else '采集任务失败，请按 job_id 查询 pipeline.log')
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    record.update(exit_code=124, status='failed', error='任务超时，已停止；结果文件将记录未完成来源，不自动补采历史日期')
                    log.error('job_timeout seconds=%s', record['timeout_seconds'])
                finally:
                    reader.join(timeout=2)
                if self.stopping and record.get('exit_code') != 0:
                    record.update(status='interrupted', error='服务停止，任务中断')
            except Exception:
                record.update(status='failed', error='执行器异常，请按 job_id 查询 service.log')
                log.exception('job_executor_failed')
            finally:
                from aris.daily_runs import finish_job
                try:
                    finish_job(record['id'],self.report_data,interrupted=record['status']!='success')
                except Exception:
                    log.exception('daily_report_finalization_failed')
                    record.update(status='failed',error='任务结果文件写入失败，请检查 service.log')
                record['finished_at'] = now()
                try:
                    self._save(record)
                    log.log(logging.INFO if record['status']=='success' else logging.ERROR,
                            'job_finished status=%s exit_code=%s', record['status'], record['exit_code'])
                finally:
                    with self.guard:
                        self.process = None
                        self.active = None

    @staticmethod
    def _capture_output(stream, record):
        with log_context(job_id=record['id'], trigger=record['trigger'], stage='worker_output'):
            try:
                while True:
                    line = stream.readline(65536)
                    if not line:
                        break
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict) and 'logger' in row and 'time' in row:
                            continue
                    except (ValueError, TypeError):
                        pass
                    log.warning('worker_output: %s', line.rstrip())
            except Exception:
                log.exception('worker_output_read_failed')
            finally:
                stream.close()

    def shutdown(self):
        with self.guard:
            self.stopping = True
            process = self.process
            thread = self.thread
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if thread is not None:
            thread.join(timeout=15)
