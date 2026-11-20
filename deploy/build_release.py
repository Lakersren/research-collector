#!/usr/bin/env python3
"""Export only the selected service + shared runtime and contracts. Never include secrets/data."""
import argparse
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[1]
IGNORED={'__pycache__','.venv','venv','data','state','logs','outputs','.git','.DS_Store'}

def build(project,output=None):
    if project not in ('research-collector','research-data-service'):raise ValueError('unknown project')
    destination=Path(output or ROOT/'dist'/(project+'.tar.gz'))
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(destination,'w:gz') as archive:
        for name in (project,'shared','contracts'):
            for path in sorted((ROOT/name).rglob('*')):
                relative=path.relative_to(ROOT)
                if path.is_symlink() or not path.is_file() or any(x in IGNORED or x.startswith('.venv-old-') for x in relative.parts):continue
                if path.name.endswith(('.env','.pyc')) or path.name.startswith('._'):continue
                # Local collection timing is server configuration, never overwrite it.
                arcname=relative.as_posix()
                if arcname == 'research-collector/config/schedule.yaml':
                    arcname += '.example'
                archive.add(path,arcname=arcname,recursive=False)
        for path in sorted((ROOT/'deploy').glob('*')):
            if path.is_file() and not path.is_symlink() and path.name != 'targets.json':archive.add(path,arcname='deploy/'+path.name,recursive=False)
        for name in ('README.md','docs/deployment.md','docs/transfer-protocol.md'):
            path=ROOT/name
            if path.exists():archive.add(path,arcname=name,recursive=False)
    return destination

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('project',choices=['research-collector','research-data-service'])
    args=parser.parse_args();print(build(args.project))
