"""Authenticated access to the files listed in a published date report."""
import json
import logging
import os
from pathlib import Path
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.responses import FileResponse

from aris.archive_source import file_record
from aris.daily_policy import normalize_target_date
from aris.paths import DATA
from corpus_contract import DailyArticle, DailyResult, DailySource, MAX_MANIFEST_BYTES, safe_path

log = logging.getLogger('corpus.transfer')


def article_paths(source):
    for article in source['articles']['success']:
        if article['text_path']:
            yield article['text_path']
        for image in article['images']:
            if image['path']:
                yield image['path']


def archive_file(root, name):
    safe_path(name)
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def install_archive_routes(app, root=None):
    root = Path(root if root is not None else DATA / 'archive').resolve()
    token = os.getenv('CORPUS_TRANSFER_TOKEN', '')
    if len(token) < 24 or token == os.getenv('CORPUS_API_TOKEN'):
        raise ValueError('CORPUS_TRANSFER_TOKEN 须至少 24 字符且与管理令牌不同')
    security = HTTPBearer(auto_error=False)

    def auth(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, '需要有效的同步令牌', headers={'WWW-Authenticate': 'Bearer'})

    def report_for(day):
        try:
            target = normalize_target_date(day)
        except ValueError:
            raise HTTPException(422, '无效目标发表日') from None
        directory = root / target.replace('-', '')
        try:
            if directory.is_symlink():
                raise FileNotFoundError(directory)
            report = json.loads(archive_file(directory, 'report.json').read_bytes())
        except FileNotFoundError:
            raise HTTPException(404, '该日期暂无已完成采集结果') from None
        except ValueError:
            raise HTTPException(500, '日期结果文件无效') from None
        if not isinstance(report, dict) or report.get('target_date') != target or report.get('version') != 1:
            raise HTTPException(500, '日期结果文件无效')
        return directory, report

    @app.get('/transfer/v1/days/{day}', dependencies=[Depends(auth)])
    def get_day(day: str):
        directory, report = report_for(day)
        try:
            sources = []
            for source in report['sources']:
                files = source.get('files')
                if files is None:
                    # Existing date archives already contain these exact files.
                    files = []
                    for name in sorted(set(article_paths(source))):
                        archive_file(directory, name)
                        files.append(file_record(directory, name))
                articles = [DailyArticle.model_validate({key: item[key] for key in DailyArticle.model_fields})
                            for item in source['articles']['success']]
                sources.append(DailySource(source_id=source['source_id'], source_name=source['source_name'],
                                           directory=source['directory'], collection=source['collection'],
                                           files=files, articles=articles))
            result = DailyResult(target_date=report['target_date'], status=report['status'], sources=sources)
            if len(result.model_dump_json().encode()) > MAX_MANIFEST_BYTES:
                raise ValueError('日期结果过大')
        except (OSError, ValueError, KeyError, TypeError):
            log.exception('daily_result_invalid day=%s', day)
            raise HTTPException(500, '日期结果文件无效') from None
        return result

    @app.get('/transfer/v1/days/{day}/files/{file_path:path}', dependencies=[Depends(auth)])
    def download(day: str, file_path: str):
        directory, report = report_for(day)
        try:
            path = archive_file(directory, file_path)
            if not any(file_path in article_paths(source) for source in report['sources']):
                raise FileNotFoundError(file_path)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, '文件不存在') from None
        return FileResponse(path, media_type='application/octet-stream',
                            headers={'Cache-Control': 'private, no-store'})
