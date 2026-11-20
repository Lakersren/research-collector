"""Local image sink used by collector. No database or object-store connection."""
from pathlib import Path
from aris.paths import RAW
from corpus_contract import image_ext

class LocalImages:
    def __init__(self,raw=None): self.raw=Path(raw or RAW)
    def key(self,source,image_key,ext):
        import hashlib
        h=hashlib.sha256(image_key.encode()).hexdigest()
        return f"web/{source.split(':',1)[-1]}/media/files/{h[:2]}/{h}.{ext}"
    def exists(self,key):
        p=(self.raw/key).resolve()
        return p.is_relative_to(self.raw.resolve()) and p.is_file() and p.stat().st_size>0
    def upload(self,key,body,content_type):
        p=(self.raw/key).resolve()
        if not p.is_relative_to(self.raw.resolve()): raise ValueError('image path escapes cache')
        ext=image_ext(body)
        if not ext or p.suffix.lstrip('.')!=ext: raise ValueError('unsupported or active image')
        p.parent.mkdir(parents=True,exist_ok=True)
        temporary=p.with_suffix(p.suffix+'.tmp');temporary.write_bytes(body);temporary.replace(p)
