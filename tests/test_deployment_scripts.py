"""Exercise deployment scripts without changing OS packages, services, or networking."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class ScriptTests(unittest.TestCase):
    def test_daily_schedule_migration_preserves_custom_settings(self):
        import yaml
        source=(ROOT/'deploy/start_service.sh').read_text()
        code=source.split("<<'PYTHON'\n")[1].split('\nPYTHON\n')[0]
        for cron,expected in [('23 * * * *','0 1 * * *'),('30 4 * * *','30 4 * * *')]:
            with self.subTest(cron=cron), tempfile.TemporaryDirectory() as tmp:
                config=Path(tmp)/'config';config.mkdir()
                path=config/'schedule.yaml'
                path.write_text(yaml.safe_dump({'timezone':'Asia/Shanghai','jobs':[
                    {'id':'collect-all','cron':cron,'enabled':False,'timeout_seconds':999},
                    {'id':'custom','cron':'23 * * * *'}]}))
                result=subprocess.run([sys.executable,'-c',code],cwd=tmp,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                settings=yaml.safe_load(path.read_text())
                self.assertEqual(settings['jobs'][0]['cron'],expected)
                self.assertFalse(settings['jobs'][0]['enabled'])
                self.assertEqual(settings['jobs'][0]['timeout_seconds'],999)
                self.assertEqual(settings['jobs'][1]['cron'],'23 * * * *')

    def test_install_preserves_old_venv_and_creates_modern_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'install.sh').write_text((ROOT/'research-collector/install.sh').read_text())
            (root/'requirements.txt').write_text('example')
            (root/'.venv/bin').mkdir(parents=True)
            old=root/'.venv/bin/python';old.write_text('#!/bin/sh\nexit 1\n');old.chmod(0o755)
            modern=root/'modern-python'
            modern.write_text('''#!/bin/bash
if [[ "$1" == -c ]]; then exit 0; fi
if [[ "$1 $2" == '-m venv' ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'STUB'
#!/bin/bash
if [[ "$1" == -c ]]; then exit 0; fi
echo "$*" >> "$TEST_PIP_LOG"
STUB
  chmod +x "$3/bin/python"
  exit 0
fi
exit 1
''');modern.chmod(0o755)
            env={**os.environ,'PYTHON_BIN':str(modern),'TEST_PIP_LOG':str(root/'pip.log')}
            result=subprocess.run(['bash',str(root/'install.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(list(root.glob('.venv-old-*'))),1)
            self.assertIn('install -r requirements.txt',(root/'pip.log').read_text())
            # Repeated install reuses the valid venv and needs no interpreter override.
            env.pop('PYTHON_BIN')
            result=subprocess.run(['bash',str(root/'install.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(list(root.glob('.venv-old-*'))),1)

    def test_render_units_uses_real_directory_and_operator_preserves_timer(self):
        source=(ROOT/'deploy/setup_service.sh').read_text()
        code=source.split("<<'PY'\n")[2].split('\nPY\n')[0]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'app space%';(root/'deploy').mkdir(parents=True)
            dest=Path(tmp)/'units';dest.mkdir()
            units=['research-collector.service','research-data-api.service','research-data-sync.service','research-data-sync.timer']
            for name in units:(root/'deploy'/name).write_text((ROOT/'deploy'/name).read_text())
            (dest/'research-data-sync.timer').write_text('custom schedule')
            code=code.replace("Path('/etc/systemd/system')",'Path('+repr(str(dest))+')')
            result=subprocess.run([sys.executable,'-c',code,str(root),'fixture-user','research',*units],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in units[:-1]:
                text=(dest/name).read_text()
                self.assertIn('User=fixture-user',text);self.assertIn('Group=research',text)
                self.assertNotIn('/opt/research-data-platform',text)
                self.assertIn('app space%%/',text)
                self.assertIn('ExecStart="',text)
                working=next(line for line in text.splitlines() if line.startswith('WorkingDirectory='))
                self.assertTrue(working.startswith('WorkingDirectory=/'),working)
                self.assertNotIn(chr(34),working)
                environment=next(line for line in text.splitlines() if line.startswith('EnvironmentFile='))
                self.assertTrue(environment.startswith('EnvironmentFile=/'),environment)
                self.assertNotIn(chr(34),environment)
            self.assertEqual((dest/'research-data-sync.timer').read_text(),'custom schedule')

    def test_collector_initial_tokens_are_distinct_and_not_printed(self):
        source=(ROOT/'deploy/setup_service.sh').read_text()
        code=source.split("<<'PY'\n")[1].split('\nPY\n')[0]
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'collector.env'
            config.write_text((ROOT/'research-collector/etc/collector.env.example').read_text())
            result=subprocess.run([sys.executable,'-c',code,str(config)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            settings=dict(line.split('=',1) for line in config.read_text().splitlines() if line and not line.startswith('#'))
            a=settings['CORPUS_API_TOKEN'];b=settings['CORPUS_TRANSFER_TOKEN']
            self.assertGreaterEqual(len(a),24);self.assertGreaterEqual(len(b),24)
            self.assertNotEqual(a,b);self.assertEqual(result.stdout,'')

if __name__=='__main__':unittest.main()
