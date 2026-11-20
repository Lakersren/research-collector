"""Export a source directly into a staged date directory."""
from datetime import datetime, timezone
import hashlib
import json
import shutil

from aris.article_audit import article_details
from aris.daily_policy import matches
from aris.kb_adapters import iter_source
from aris.paths import RAW
from aris.web_media import SiteStore
from corpus_contract import Article, Collection, File, digest


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def file_record(root, path):
    local = root / path
    return File(path=path, size=local.stat().st_size, sha256=digest(local)).model_dump()


def export_source(source, directory, folder, target_date):
    ledger = SiteStore(source['key']).load_ledger()
    by_key = {entry['file_key']: entry for entry in ledger.values() if entry.get('file_key')}
    seen = set()
    saved = []
    files = {}
    (directory / folder / 'images').mkdir(parents=True)
    for doc, images in iter_source(source):
        if not matches(doc['published_at'], target_date):
            raise ValueError('文章发布日期不属于目标日')
        if doc['id'] in seen:
            raise ValueError('文章 ID 重复')
        seen.add(doc['id'])
        doc['published_at'] = doc['published_at'].replace(tzinfo=timezone.utc).isoformat()
        relative_images = []
        for image in images:
            path = None
            if image['status'] == 'ok':
                entry = by_key.get(image['file_key'], {})
                local = (RAW / (entry.get('file') or image['file_key'])).resolve()
                if not local.is_relative_to(RAW) or not local.is_file():
                    raise ValueError('图片缓存缺失或路径越界')
                path = f'images/{digest(local)}{local.suffix.lower()}'
                relative = f'{folder}/{path}'
                if relative not in files:
                    shutil.copyfile(local, directory / relative)
                    files[relative] = file_record(directory, relative)
            relative_images.append(dict(position=image['position'], status=image['status'], path=path,
                                        width=image['width'], height=image['height']))
        article = Article.model_validate({**doc, 'images': relative_images})
        metadata = article.model_dump(exclude={'content', 'source_id'})
        for image in metadata['images']:
            if image['path']:
                image['path'] = f"{folder}/{image['path']}"
        text_path = None
        if article.content is not None:
            text_path = f'{folder}/{hashlib.sha256(article.id.encode()).hexdigest()}.txt'
            with (directory / text_path).open('x', encoding='utf-8') as stream:
                stream.write(article.content)
            files[text_path] = file_record(directory, text_path)
        saved.append({**article_details(article.title, article.url, article.published_at), **metadata,
                      'text_path': text_path,
                      'images_failed': sum(image.status == 'failed' for image in article.images)})
    return saved, list(files.values())


def source_result(source, directory, started_at, saved=(), files=(), counts=None, errors=(), audit=None):
    articles = {'success': list(saved), 'failed': [], 'skipped': []}
    if audit is not None and audit.is_file():
        saved_urls = {item['url'] for item in saved}
        for line in audit.read_text(encoding='utf-8').splitlines():
            item = json.loads(line)
            status = item.pop('status')
            if status == 'fetched':
                if item['url'] in saved_urls:
                    continue
                status = 'failed'
                item['reason'] = '文章已抓取，归档未完成'
            articles[status].append(item)
    counts = counts or {}
    collection = Collection(started_at=started_at, finished_at=stamp(),
                            n_discovered=counts.get('n_index', 0), n_fetched=counts.get('n_fetched', 0),
                            n_failed=max(counts.get('n_failed', 0), len(articles['failed'])),
                            status='failed' if errors else 'success',
                            error='; '.join(errors)[:4000] or None)
    return dict(source_id=source['key'], source_name=source['name'], directory=directory,
                collection=collection.model_dump(), files=list(files), articles=articles,
                article_counts={status: len(rows) for status, rows in articles.items()})
