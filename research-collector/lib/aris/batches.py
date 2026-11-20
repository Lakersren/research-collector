"""Immutable transfer batches, published by atomic rename after all files are ready."""
from datetime import datetime,timezone
import json
import shutil
import uuid
from pathlib import Path
from aris.daily_policy import matches
from corpus_contract import Manifest, Article, File, digest, MAX_ARTICLE_LINE_BYTES
from aris.paths import DATA, RAW
from aris.kb_adapters import iter_source
from aris.web_media import SiteStore

BATCHES=DATA/'batches'

def stamp(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')

def publish(source,counts,errors,started_at,root=None):
    root=Path(root or BATCHES);root.mkdir(parents=True,exist_ok=True)
    batch_id=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')+'-'+uuid.uuid4().hex
    temporary=root/('.'+batch_id);temporary.mkdir()
    try:
        ledger=SiteStore(source['key']).load_ledger()
        by_key={row.get('file_key'):row for row in ledger.values() if row.get('file_key')}
        with (temporary/'articles.jsonl').open('w',encoding='utf-8') as out:
            for doc,images in iter_source(source):
                if not matches(doc.get("published_at")):
                    raise ValueError("文章发布日期不属于本轮昨天窗口，拒绝发布")
                transfers=[]
                for image in images:
                    path=None;status=image['status']
                    if status=='ok':
                        row=by_key.get(image['file_key'],{})
                        local=(RAW/(row.get('file') or image['file_key'])).resolve()
                        if not local.is_relative_to(RAW.resolve()) or not local.is_file():
                            raise ValueError('image ledger references a missing local image; recollect before publishing')
                        ext=local.suffix.lower()
                        path='images/'+digest(local)+ext
                        target=temporary/path;target.parent.mkdir(exist_ok=True)
                        if not target.exists(): shutil.copyfile(local,target)
                    transfers.append(dict(position=image['position'],status=status,path=path,
                                          width=image['width'],height=image['height']))
                doc['published_at']=doc['published_at'].replace(tzinfo=timezone.utc).isoformat() if doc['published_at'] else None
                article=Article.model_validate({**doc,'images':transfers})
                line=article.model_dump_json()+'\n'
                if len(line.encode('utf-8'))>MAX_ARTICLE_LINE_BYTES:raise ValueError('article exceeds transfer limit')
                out.write(line)
            # Empty collections still carry one valid newline, not an empty forbidden file.
            if out.tell()==0: out.write('\n')
        files=[File(path=p.relative_to(temporary).as_posix(),size=p.stat().st_size,sha256=digest(p))
               for p in sorted(temporary.rglob('*')) if p.is_file()]
        manifest=Manifest(id=batch_id,source_id=source['key'],created_at=stamp(),files=files,
            collection=dict(started_at=started_at,finished_at=stamp(),n_discovered=counts.get('n_index',0),
                            n_fetched=counts.get('n_fetched',0),n_failed=counts.get('n_failed',0),
                            status='failed' if errors else 'success',error='; '.join(errors)[:4000] or None))
        (temporary/'manifest.json').write_text(manifest.model_dump_json(indent=2),encoding='utf-8')
        temporary.rename(root/batch_id)
        return manifest
    except BaseException:
        shutil.rmtree(temporary,ignore_errors=True)
        raise
