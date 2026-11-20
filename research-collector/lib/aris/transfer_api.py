"""Authenticated, fixed-root downloads. A never accepts SQL or internal credentials."""
import json
import logging
import uuid
import os
from pathlib import Path
import re
import secrets
import time
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from starlette.responses import FileResponse
from corpus_contract import RunReport,Manifest,safe_path,MAX_MANIFEST_BYTES
from aris.batches import BATCHES
from aris.paths import STATE

log=logging.getLogger('corpus.transfer')
ID=re.compile(r'^\d{20}-[0-9a-f]{32}$')

def install_batch_routes(app,root=None):
    root=Path(root or BATCHES).resolve()
    token=os.getenv('CORPUS_TRANSFER_TOKEN','')
    if len(token)<24 or token==os.getenv('CORPUS_API_TOKEN'):
        raise ValueError('CORPUS_TRANSFER_TOKEN 须至少 24 字符且与管理令牌不同')
    security=HTTPBearer(auto_error=False)
    def auth(credentials:HTTPAuthorizationCredentials|None=Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(),token.encode()):
            raise HTTPException(401,'需要有效的同步令牌',headers={'WWW-Authenticate':'Bearer'})
    def manifest(batch_id):
        if not ID.fullmatch(batch_id): raise HTTPException(404,'批次不存在')
        directory=root/batch_id
        p=directory/'manifest.json'
        if directory.is_symlink() or p.is_symlink() or not p.is_file(): raise HTTPException(404,'批次不存在')
        if p.stat().st_size>MAX_MANIFEST_BYTES: raise HTTPException(500,'批次清单过大')
        try:
            value=Manifest.model_validate_json(p.read_bytes())
        except ValueError:
            log.exception('batch_manifest_invalid batch_id=%s',batch_id)
            raise HTTPException(500,'批次清单无效，请联系管理员') from None
        if value.id!=batch_id: raise HTTPException(500,'批次标识不一致')
        return value
    @app.middleware('http')
    async def transfer_log(request,call_next):
        if not request.url.path.startswith('/transfer/'):
            return await call_next(request)
        from aris.observability import log_context
        rid=str(uuid.uuid4());started=time.monotonic()
        with log_context(request_id=rid,interface='transfer'):
            response=await call_next(request)
            response.headers['X-Request-ID']=rid
            response.headers['Cache-Control']='private, no-store'
            log.info('transfer_finished method=%s status=%s duration_seconds=%.3f',
                     request.method,response.status_code,time.monotonic()-started)
            return response

    reports=root.parent/'runs'
    @app.get('/transfer/v1/runs',dependencies=[Depends(auth)])
    def runs(after:str='',limit:int=Query(default=100,ge=1,le=100)):
        if after and not ID.fullmatch(after):raise HTTPException(422,'无效任务游标')
        ids=sorted(p.stem for p in reports.glob('*.json') if ID.fullmatch(p.stem) and p.stem>after
                   and p.is_file() and not p.is_symlink())
        selected=ids[:limit]
        return {'version':1,'items':selected,'has_more':len(ids)>limit,
                'next_cursor':selected[-1] if len(ids)>limit else None}
    @app.get('/transfer/v1/runs/{run_id}',dependencies=[Depends(auth)])
    def get_run(run_id:str):
        if not ID.fullmatch(run_id):raise HTTPException(404,'任务结果不存在')
        p=reports/(run_id+'.json')
        if p.is_symlink() or not p.is_file():raise HTTPException(404,'任务结果不存在')
        try:
            payload = json.loads(p.read_bytes())
            for source in payload['sources']:
                source.pop('articles', None)  # Only the local report contains article audit details.
            value = RunReport.model_validate(payload)
            if len(value.model_dump_json().encode()) > MAX_MANIFEST_BYTES:
                raise HTTPException(500,'任务结果文件过大')
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(500,'任务结果文件无效') from None
        if value.id!=run_id:raise HTTPException(500,'任务结果标识不一致')
        return value.model_dump()

    @app.get('/transfer/v1/batches',dependencies=[Depends(auth)])
    def batches(after:str='',limit:int=Query(default=100,ge=1,le=100)):
        if after and not ID.fullmatch(after): raise HTTPException(422,'无效批次游标')
        ids=sorted(p.name for p in root.glob('*') if ID.fullmatch(p.name) and p.name>after
                   and p.is_dir() and not p.is_symlink() and (p/'manifest.json').is_file())
        selected=ids[:limit]
        return {'version':1,'items':selected,'has_more':len(ids)>limit,
                'next_cursor':selected[-1] if len(ids)>limit else None}
    @app.get('/transfer/v1/batches/{batch_id}',dependencies=[Depends(auth)])
    def get_manifest(batch_id:str): return manifest(batch_id).model_dump()
    @app.get('/transfer/v1/batches/{batch_id}/files/{file_path:path}',dependencies=[Depends(auth)])
    def download(batch_id:str,file_path:str):
        value=manifest(batch_id)
        try: safe_path(file_path)
        except ValueError: raise HTTPException(404,'文件不存在') from None
        entry=next((x for x in value.files if x.path==file_path),None)
        directory=root/batch_id;p=(directory/file_path).resolve()
        if not entry or not p.is_relative_to(directory.resolve()) or not p.is_file() or p.stat().st_size!=entry.size:
            raise HTTPException(404,'文件不存在')
        return FileResponse(p,media_type='application/octet-stream',headers={'Cache-Control':'private, no-store'})
    @app.post('/transfer/v1/batches/{batch_id}/ack',dependencies=[Depends(auth)])
    def ack(batch_id:str):
        manifest(batch_id)
        directory=STATE/'transfer-acks';directory.mkdir(parents=True,exist_ok=True)
        p=directory/(batch_id+'.json')
        # Idempotent acknowledgment; packages are retained, never removed during download.
        if not p.exists():
            tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps({'id':batch_id,'received_at':time.time()}));tmp.replace(p)
        return {'id':batch_id,'acknowledged':True}
