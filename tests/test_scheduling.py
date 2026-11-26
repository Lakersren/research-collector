"""Scheduling, HTTP control, crash recovery and redacted diagnostics."""
from datetime import datetime
import io
import json
import logging
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'research-collector'
sys.path[:0] = [str(REPO/'research-data-service'),str(REPO/'research-data-service/lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
os.environ.setdefault('ARIS_ROOT', str(ROOT))
sys.path[:0] = [str(ROOT), str(ROOT / 'lib')]
from apscheduler.events import EVENT_JOB_REMOVED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from fastapi.testclient import TestClient
from aris.dispatch import BusyError, Dispatcher
from aris.observability import JsonFormatter, log_context
import run
import service


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_overlapping_submissions_rejected_and_result_persisted(self):
        entered = threading.Event()
        release = threading.Event()
        process = Mock(stdout=io.StringIO(''))
        def wait(timeout=None):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timed out')
            return 0
        process.wait.side_effect = wait
        with patch('aris.dispatch.subprocess.Popen', return_value=process) as factory:
            dispatcher = Dispatcher(self.state)
            try:
                record = dispatcher.submit(sources=['servethehome'])
                self.assertTrue(entered.wait(3))
                with self.assertRaises(BusyError):
                    dispatcher.submit(sources=['servethehome'])
                self.assertIn('--job-id', factory.call_args.args[0])
                self.assertNotIn('MYSQL_PASSWORD', ' '.join(factory.call_args.args[0]))
            finally:
                release.set()
                dispatcher.thread.join(3)
            saved = dispatcher.get(record['id'])
            self.assertEqual(saved['status'], 'success')
            self.assertEqual(saved['exit_code'], 0)
            self.assertIsNone(dispatcher.active)
            self.assertEqual(Dispatcher(self.state).get(record['id'])['status'], 'success')

    def test_timeout_kills_process_and_records_failure(self):
        process = Mock(stdout=io.StringIO(''))
        process.wait.side_effect = [subprocess.TimeoutExpired(['worker'], 1),
                                    subprocess.TimeoutExpired(['worker'], 10), -9]
        with patch('aris.dispatch.subprocess.Popen', return_value=process):
            dispatcher = Dispatcher(self.state)
            record = dispatcher.submit(sources=['servethehome'], timeout_seconds=1)
            dispatcher.thread.join(3)
        result = dispatcher.get(record['id'])
        self.assertEqual(result['exit_code'], 124)
        self.assertEqual(result['status'], 'failed')
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_partial_worker_status_is_preserved(self):
        process = Mock(stdout=io.StringIO(''))
        process.wait.return_value = 3
        with patch('aris.dispatch.subprocess.Popen', return_value=process):
            dispatcher = Dispatcher(self.state)
            record = dispatcher.submit(sources=['servethehome'])
            dispatcher.thread.join(3)
        self.assertEqual(dispatcher.get(record['id'])['status'], 'partial_success')

    def test_worker_failure_is_not_reported_as_success(self):
        process = Mock(stdout=io.StringIO(''))
        process.wait.return_value = 75
        with patch('aris.dispatch.subprocess.Popen', return_value=process):
            dispatcher = Dispatcher(self.state)
            record = dispatcher.submit(sources=['servethehome'])
            dispatcher.thread.join(3)
        self.assertEqual(dispatcher.get(record['id'])['status'], 'failed')

    def test_shutdown_terminates_active_worker_and_persists_interruption(self):
        entered = threading.Event()
        released = threading.Event()
        process = Mock(stdout=io.StringIO(''))
        process.poll.return_value = None
        def wait(timeout=None):
            entered.set()
            if not released.wait(3):
                raise RuntimeError('test timeout')
            return -15
        process.wait.side_effect = wait
        process.terminate.side_effect = released.set
        with patch('aris.dispatch.subprocess.Popen', return_value=process):
            dispatcher = Dispatcher(self.state)
            record = dispatcher.submit(sources=['servethehome'])
            self.assertTrue(entered.wait(3))
            dispatcher.shutdown()
        self.assertEqual(dispatcher.get(record['id'])['status'], 'interrupted')
        process.terminate.assert_called_once()

    def test_early_worker_errors_are_captured_without_duplicating_json_logs(self):
        process = Mock(stdout=io.StringIO('ModuleNotFoundError: missing_driver\n'
                                        '{"time":"now","logger":"corpus.pipeline","message":"already written"}\n'))
        process.wait.return_value = 1
        with patch('aris.dispatch.subprocess.Popen', return_value=process), \
             self.assertLogs('corpus.dispatch', level='INFO') as capture:
            dispatcher = Dispatcher(self.state)
            record = dispatcher.submit(sources=['servethehome'])
            dispatcher.thread.join(3)
        self.assertEqual(dispatcher.get(record['id'])['status'], 'failed')
        text='\n'.join(capture.output)
        self.assertIn('missing_driver', text)
        self.assertNotIn('already written', text)

    def test_restart_marks_unfinished_task_interrupted(self):
        jobs = self.state / 'jobs'
        jobs.mkdir()
        job_id = str(uuid.uuid4())
        (jobs / (job_id+'.json')).write_text(json.dumps({'id':job_id, 'status':'running'}))
        dispatcher = Dispatcher(self.state)
        self.assertEqual(dispatcher.get(job_id)['status'], 'interrupted')
        with self.assertRaises(KeyError):
            dispatcher.get('../../etc/app.env')

    def test_unknown_source_is_rejected_before_subprocess(self):
        with patch('aris.dispatch.subprocess.Popen') as factory:
            dispatcher = Dispatcher(self.state)
            with self.assertRaises(ValueError):
                dispatcher.submit(sources=['typo'])
            factory.assert_not_called()
            self.assertEqual(dispatcher.recent(), [])

    def test_cli_respects_same_pipeline_lock(self):
        import fcntl
        with (self.state/'pipeline.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(run, 'STATE', self.state), patch('run.setup_logging'), patch('run.process') as process:
                self.assertEqual(run.main(['collect', '--source', 'servethehome']), 75)
                process.assert_not_called()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        config = self.path / 'schedule.yaml'
        config.write_text('timezone: Asia/Shanghai\njobs: []\n')
        self.dispatcher = Mock()
        self.dispatcher.active = None
        self.dispatcher.recent.return_value = []
        self.token = 'local-test-token-at-least-24-chars'
        self.headers = {'Authorization': 'Bearer '+self.token}
        self.env = patch.dict(os.environ, {'CORPUS_API_TOKEN': self.token,'CORPUS_TRANSFER_TOKEN':'transfer-token-for-tests-32-chars'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.state = patch.object(service, 'STATE', self.path)
        self.state.start(); self.addCleanup(self.state.stop)
        self.app = service.create_app(config, dispatcher_factory=lambda: self.dispatcher)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_auth_required_and_health_readable(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/jobs').status_code, 401)
        self.assertEqual(self.client.get('/jobs', headers={'Authorization':'Bearer wrong'}).status_code, 401)
        self.assertEqual(self.client.get('/sources', headers=self.headers).status_code, 200)
        self.assertEqual(len(self.client.get('/sources', headers=self.headers).json()), 29)

    def test_submit_validates_request_and_reports_busy(self):
        self.dispatcher.submit.return_value = {'id':'test', 'status':'queued'}
        result = self.client.post('/jobs', headers=self.headers, json={'sources':['servethehome'], 'limit':1})
        self.assertEqual(result.status_code, 202)
        self.assertEqual(self.dispatcher.submit.call_args.kwargs['trigger'], 'api')
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={'limit':0}).status_code, 422)
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={'command':'shell'}).status_code, 422)
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={'sources':['servethehome'],'limit':'2'}).status_code, 422)
        self.dispatcher.submit.side_effect = BusyError('busy')
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={}).status_code, 409)
        self.dispatcher.submit.side_effect = ValueError('unknown source')
        self.assertEqual(self.client.post('/jobs',headers=self.headers,json={}).status_code, 422)

    def test_job_lookup_and_not_found(self):
        self.dispatcher.get.return_value = {'id':'job', 'status':'success'}
        self.assertEqual(self.client.get('/jobs/job',headers=self.headers).json()['status'], 'success')
        self.dispatcher.get.side_effect = KeyError('missing')
        self.assertEqual(self.client.get('/jobs/missing',headers=self.headers).status_code, 404)

    def test_today_trigger_accepts_only_all_or_one_source(self):
        self.dispatcher.submit.return_value = {'id': 'test', 'target_date': '2026-11-19', 'status': 'queued'}
        for payload, sources in [
            ({'scope': 'all'}, []),
            ({'scope': 'source', 'source_id': 'web:servethehome'}, ['web:servethehome']),
        ]:
            with self.subTest(payload=payload):
                result = self.client.post('/jobs/today', headers=self.headers, json=payload)
                self.assertEqual(result.status_code, 202, result.text)
                self.assertEqual(result.json()['target_date'], '2026-11-19')
                self.dispatcher.submit.assert_called_with(command='collect', sources=sources, trigger='api')

        self.dispatcher.submit.reset_mock()
        for invalid in ({}, {'scope': 'source'}, {'scope': 'source', 'source_id': ''},
                        {'scope': 'all', 'source_id': 'web:servethehome'},
                        {'scope': 'all', 'limit': 1}, {'scope': 'all', 'target_date': '2026-11-18'}):
            self.assertEqual(self.client.post('/jobs/today', headers=self.headers, json=invalid).status_code, 422)
        self.assertEqual(self.client.post('/jobs/today', json={'scope': 'all'}).status_code, 401)
        self.dispatcher.submit.assert_not_called()
        self.dispatcher.submit.side_effect = BusyError('busy')
        self.assertEqual(self.client.post('/jobs/today', headers=self.headers, json={'scope': 'all'}).status_code, 409)
        self.dispatcher.submit.side_effect = ValueError('unknown source')
        result = self.client.post('/jobs/today', headers=self.headers, json={'scope': 'source', 'source_id': 'typo'})
        self.assertEqual(result.status_code, 422)


class ScheduleAndLoggingTests(unittest.TestCase):
    def test_default_schedule_is_daily_in_shanghai(self):
        jobs = service.load_schedule(ROOT/'config/schedule.yaml')
        self.assertEqual(len(jobs), 1)
        item, trigger = jobs[0]
        self.assertEqual(len(run.select_sources(item.sources)), 29)
        start = datetime(2026,11,17,10,0,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(trigger.get_next_fire_time(None,start), start.replace(day=18,hour=1,minute=0))

    def test_real_scheduler_clock_calls_scheduled_submission(self):
        dispatched = threading.Event()
        dispatcher = Mock()
        dispatcher.submit.side_effect = lambda **kw: (dispatched.set() or {'id':'test'})
        item = service.ScheduleJob(id='test', cron='* * * * *', sources=['servethehome'])
        scheduler = BackgroundScheduler()
        removed = threading.Event()
        scheduler.add_listener(lambda event: removed.set(), EVENT_JOB_REMOVED)
        scheduler.add_job(service.scheduled_submit, DateTrigger(run_date=datetime.now()), args=[dispatcher,item])
        scheduler.start()
        try:
            self.assertTrue(dispatched.wait(3))
            self.assertTrue(removed.wait(3))
            self.assertEqual(dispatcher.submit.call_args.kwargs['trigger'], 'schedule')
        finally:
            scheduler.shutdown()
        dispatcher.submit.side_effect = BusyError('busy')
        service.scheduled_submit(dispatcher, item)  # Busy tick is skipped, not queued or crashed.

    def test_schedule_rejects_unknown_source_duplicate_ids_and_bad_cron(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'schedule.yaml'
            for jobs in [
                [{'id':'a','cron':'* * * * *','sources':['typo']}],
                [{'id':'a','cron':'bad'}],
                [{'id':'a','cron':'* * * * *'},{'id':'a','cron':'* * * * *'}],
            ]:
                path.write_text(json.dumps({'jobs':jobs}))
                with self.assertRaises(ValueError): service.load_schedule(path)

    def test_log_context_traceback_and_secrets_are_redacted(self):
        formatter = JsonFormatter(secrets=['very-secret-value'])
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        logger = logging.getLogger('redaction-test')
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        with log_context(job_id='job-1', source_id='web:test', stage='images'):
            try:
                raise RuntimeError('very-secret-value https://user:pass@example.test/image?X-Amz-Signature=abc')
            except RuntimeError:
                logger.exception('image failed')
        text = stream.getvalue()
        self.assertNotIn('very-secret-value', text)
        self.assertNotIn('user:pass', text)
        self.assertNotIn('Signature=abc', text)
        row = json.loads(text)
        self.assertEqual(row['job_id'], 'job-1')
        self.assertEqual(row['source_id'], 'web:test')
        self.assertIn('RuntimeError', row['exception'])


if __name__ == '__main__': unittest.main()
