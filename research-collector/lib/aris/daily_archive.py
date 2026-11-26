"""Publish one date directory at a time and retain previous files on failure."""
import ctypes
from datetime import datetime, timedelta
import json
import os
import re
import shutil

from aris.completion_report import render
from aris.daily_policy import SHANGHAI


def source_directory(source_id):
    from hashlib import sha256

    name = source_id.removeprefix('web:')
    if re.fullmatch(r'[A-Za-z0-9_-]{1,191}', name) and source_id.startswith('web:'):
        return name
    return '~' + sha256(source_id.encode()).hexdigest()


def exchange_directories(staging, destination):
    # Linux can replace a nonempty date directory without a gap at its public path.
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


PARTIAL_SUCCESS_EXIT_CODE = 3


def collection_status(sources):
    if all(source['collection']['status'] == 'success' for source in sources):
        return 'success'
    if any(source['collection']['status'] == 'success' or source['articles']['success']
           for source in sources):
        return 'partial_success'
    return 'failed'


def publish_date(data, staging, attempt, replace_all=False):
    root = data / 'archive'
    root.mkdir(exist_ok=True)
    destination = root / attempt['target_date'].replace('-', '')
    exists = destination.is_dir()
    current = {source['source_id']: source for source in attempt['sources']}
    saved = attempt.copy()
    if exists:
        previous = json.loads((destination / 'report.json').read_text(encoding='utf-8'))
        keep_previous = attempt['status'] in ('failed', 'interrupted')
        if keep_previous:
            for source in current.values():
                folder = staging / source['directory']
                if folder.is_dir():
                    shutil.rmtree(folder)
            saved = {key: value for key, value in previous.items() if key not in ('id', 'latest_attempt')}
            current.clear()
        if keep_previous or not replace_all or attempt['status'] == 'partial_success':
            for source in previous['sources']:
                source_id = source['source_id']
                replacement = current.get(source_id)
                if replacement and (replacement['collection']['status'] == 'success'
                                    or replacement['articles']['success']
                                    or not source['articles']['success']):
                    continue
                if not replacement and replace_all and not keep_previous:
                    continue
                for key in ('batch_id', 'run_id', 'run_finished_at'):
                    source.pop(key, None)
                folder = source['directory']
                if replacement:
                    shutil.rmtree(staging / folder)
                shutil.copytree(destination / folder, staging / folder, copy_function=os.link)
                current[source_id] = source

    saved['sources'] = list(current.values())
    if attempt['status'] in ('success', 'partial_success'):
        saved['status'] = collection_status(saved['sources'])
    # The latest attempt is useful even when its files were discarded.
    saved['latest_attempt'] = {
        **{key: value for key, value in attempt.items() if key != 'sources'},
        'sources': [
            {**{key: source[key] for key in ('source_id', 'source_name', 'collection', 'article_counts')},
             'articles': {status: source['articles'][status] for status in ('failed', 'skipped')}}
            for source in attempt['sources']
        ],
    }
    (staging / 'report.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
    (staging / 'report.md').write_text(render(saved), encoding='utf-8')
    if exists:
        exchange_directories(staging, destination)
    else:
        staging.rename(destination)
    return destination


def prune_dates(data, today=None):
    root = data / 'archive'
    if not root.is_dir():
        return
    cutoff = ((today or datetime.now(SHANGHAI).date()) - timedelta(days=8)).strftime('%Y%m%d')
    for entry in root.iterdir():
        if (re.fullmatch(r'[0-9]{8}', entry.name) and entry.name <= cutoff
                and entry.is_dir() and not entry.is_symlink()):
            shutil.rmtree(entry)
