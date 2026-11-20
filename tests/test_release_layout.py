"""Deployment archives must boot independently and exclude runtime data and real secrets."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_builder',ROOT/'deploy/build_release.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class ReleaseTests(unittest.TestCase):
    def test_each_archive_boots_without_other_service_or_secrets(self):
        for project,entry in [('research-collector','service.py'),('research-data-service','api_service.py')]:
            with self.subTest(project=project),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);archive=builder.build(project,directory/'release.tar.gz')
                with tarfile.open(archive) as bundle:
                    names=bundle.getnames()
                    other='research-data-service' if project=='research-collector' else 'research-collector'
                    self.assertFalse(any(x.startswith(other+'/') for x in names))
                    self.assertFalse(any(x.endswith('.env') for x in names))
                    self.assertIn(project+'/start.sh',names)
                    self.assertIn(project+'/setup.sh',names)
                    self.assertIn(project+'/install.sh',names)
                    self.assertIn('deploy/setup_service.sh',names)
                    self.assertIn('deploy/start_service.sh',names)
                    self.assertNotIn('deploy/update.py',names)
                    self.assertNotIn('deploy/push.py',names)
                    if project=='research-collector':
                        self.assertNotIn(project+'/config/schedule.yaml',names)
                        self.assertIn(project+'/config/schedule.yaml.example',names)
                    self.assertFalse(any(any(v in Path(x).parts for v in ('data','logs','state','.venv','__pycache__')) for x in names))
                    bundle.extractall(directory/'release',filter='data')
                root=directory/'release'/project
                env={k:v for k,v in os.environ.items() if not k.startswith(('ARIS_','CORPUS_','MYSQL_','XSKY_'))}
                result=subprocess.run([sys.executable,entry,'--help'],cwd=root,env=env,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                module=entry.removesuffix('.py')
                if project=='research-collector':
                    script=f'import {module}; import sys; assert "pymysql" not in sys.modules; assert "boto3" not in sys.modules; assert "mcp" not in sys.modules'
                else:
                    script=f'import {module}; import sys; assert "collectors" not in sys.modules; assert "trafilatura" not in sys.modules; assert "aris.dispatch" not in sys.modules'
                result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
