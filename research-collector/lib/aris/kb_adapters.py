"""Normalize collector text manifests into the four-table schema."""
import json
from datetime import datetime, timezone
from aris.paths import RAW
from aris.source_config import sources
from aris.web_media import SiteStore

def jsonl(path):
    with path.open(encoding='utf-8') as f:
        for number, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as e:
                    raise ValueError(f'{path.name} 第 {number} 行不是合法 JSON') from e

def published(value):
    if not value:
        return None
    return _utc(value)

def _utc(value):
    d = datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(tzinfo=None)

def iter_source(source):
    store = SiteStore(source['key'])
    ledger = store.load_ledger()
    for d in jsonl(store.base / 'text' / 'index.jsonl'):
        pid = d.get('post_id') or d.get('item_id')
        if pid is None or not d.get('url'):
            raise ValueError('文章缺少 ID 或 URL')
        state = d.get('content_state') or ('preview' if d.get('is_preview') else 'ready')
        if state not in ('ready','preview','no_text'):
            raise ValueError('未知正文状态')
        text = ''
        if d.get('text_path'):
            p = (RAW / d['text_path']).resolve()
            if not p.is_relative_to(RAW.resolve()):
                raise ValueError('正文路径越界')
            text = p.read_text(encoding='utf-8')
        if not text.strip():
            if state != 'no_text' and d.get('chars',0):
                raise ValueError('非空正文缺失，拒绝覆盖已有文章')
            state = 'no_text'
        site = source['key'].split(':',1)[-1]
        docid = f"semianalysis:{d.get('site') or 'substack'}:{pid}" if source['_seg']=='semi' else f'web:{site}:{pid}'
        authors = d.get('authors') or []
        if not isinstance(authors, list) or any(not isinstance(a,str) for a in authors):
            raise ValueError('authors 必须是字符串数组')
        doc = dict(id=docid, source_id=source['key'], title=d.get('title') or '(untitled)',
                   authors=authors, published_at=published(d.get('post_date')), url=d['url'],
                   content_state=state, content=text if state != 'no_text' else None)
        images=[]
        positions=set()
        for i, im in enumerate(d.get('images') or [],1):
            n=int(im.get('n',i))
            if n<1 or n in positions:
                raise ValueError('图片序号重复或无效')
            positions.add(n)
            row=ledger.get(im['k'],{})
            key=row.get('file_key') if row.get('status')=='ok' else None
            images.append(dict(doc_id=docid, position=n, file_key=key,
                               status='ok' if key else ('failed' if row.get('status')=='failed' else 'pending'),
                               width=im.get('w') or row.get('w'), height=im.get('h') or row.get('h')))
        yield doc, images
