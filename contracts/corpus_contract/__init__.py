"""Versioned transfer contract. Payloads are data, never executable SQL."""
from datetime import datetime
import hashlib
import re
from pathlib import PurePosixPath
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VERSION=1
MAX_FILE_BYTES=256*1024*1024
MAX_BATCH_BYTES=4*1024*1024*1024
MAX_MANIFEST_BYTES=8*1024*1024
MAX_ARTICLE_LINE_BYTES=16*1024*1024

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True)

def safe_path(value):
    p=PurePosixPath(value)
    if not value or '\\' in value or p.is_absolute() or '..' in p.parts or str(p)!=value or any(x in value for x in ('\x00','\r','\n')):
        raise ValueError('invalid relative file path')
    return value

def valid_time(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('timezone required')
    return value

class File(Strict):
    path:str
    size:int=Field(ge=1,le=MAX_FILE_BYTES)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    _path=field_validator('path')(safe_path)

class Collection(Strict):
    started_at:str
    finished_at:str
    n_discovered:int=Field(ge=0)
    n_fetched:int=Field(ge=0)
    n_failed:int=Field(ge=0)
    status:Literal['success','failed']
    error:str|None=Field(default=None,max_length=4000)
    _time=field_validator('started_at','finished_at')(valid_time)
    @model_validator(mode='after')
    def validate_order(self):
        start=datetime.fromisoformat(self.started_at.replace('Z','+00:00'))
        end=datetime.fromisoformat(self.finished_at.replace('Z','+00:00'))
        if end<start:raise ValueError('collection finished before it started')
        return self

class Manifest(Strict):
    version:Literal[1]=1
    id:str=Field(pattern=r'^\d{20}-[0-9a-f]{32}$')
    source_id:str=Field(min_length=1,max_length=191)
    created_at:str
    collection:Collection
    files:list[File]=Field(min_length=1,max_length=50000)
    _time=field_validator('created_at')(valid_time)
    @model_validator(mode='after')
    def validate_files(self):
        paths=[x.path for x in self.files]
        if len(set(paths))!=len(paths) or paths.count('articles.jsonl')!=1:
            raise ValueError('missing articles or duplicate file')
        if sum(x.size for x in self.files)>MAX_BATCH_BYTES: raise ValueError('batch too large')
        if any(x!='articles.jsonl' and not x.startswith('images/') for x in paths):
            raise ValueError('unexpected file')
        return self

class Image(Strict):
    position:int=Field(ge=1)
    status:Literal['ok','failed','pending']
    path:str|None=None
    width:int|None=Field(default=None,ge=1)
    height:int|None=Field(default=None,ge=1)
    @model_validator(mode='after')
    def validate_file(self):
        if self.status=='ok':
            if not self.path or not safe_path(self.path).startswith('images/'): raise ValueError('ok image requires local file')
        elif self.path is not None: raise ValueError('unavailable image must have null path')
        return self

class Article(Strict):
    id:str=Field(min_length=1,max_length=191)
    source_id:str=Field(min_length=1,max_length=191)
    title:str=Field(min_length=1)
    authors:list[str]
    published_at:str|None
    url:str=Field(pattern=r'^https?://')
    content_state:Literal['ready','preview','no_text']
    content:str|None
    images:list[Image]
    @model_validator(mode='after')
    def validate_article(self):
        if self.published_at is not None: valid_time(self.published_at)
        if self.content_state=='no_text':
            if self.content is not None: raise ValueError('no_text content must be null')
        elif not self.content or not self.content.strip(): raise ValueError('body missing')
        positions=[x.position for x in self.images]
        if len(positions)!=len(set(positions)): raise ValueError('duplicate image position')
        return self

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def image_ext(body):
    """Supported inert image formats shared by A and B. Never accept HTML by MIME alone."""
    for magic,ext in ((b'\x89PNG\r\n\x1a\n','png'),(b'\xff\xd8\xff','jpg'),
                      (b'GIF87a','gif'),(b'GIF89a','gif'),(b'BM','bmp'),
                      (b'II*\x00','tiff'),(b'MM\x00*','tiff')):
        if body.startswith(magic):return ext
    if body[:4]==b'RIFF' and body[8:12]==b'WEBP':return 'webp'
    if body[4:8]==b'ftyp' and any(brand in body[8:32] for brand in (b'avif',b'avis')):return 'avif'
    if b'<svg' not in body[:4096].lower():return None
    # Preserve static SVG charts, but do not carry executable markup or remote dependencies inside them.
    if b'<!doctype' in body.lower() or b'<!entity' in body.lower():return None
    import xml.etree.ElementTree as ET
    try:root=ET.fromstring(body)
    except (ET.ParseError,ValueError):return None
    if root.tag.split('}')[-1]!='svg':return None
    for node in root.iter():
        tag=node.tag.split('}')[-1].lower()
        if tag in ('script','foreignobject','iframe','object','embed','animate','set'):return None
        for key,value in node.attrib.items():
            key=key.split('}')[-1].lower();value=value.strip()
            if key.startswith('on') or 'javascript:' in value.lower():return None
            if key in ('href','src') and not (value.startswith('#') or value.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,'))):return None
        css=' '.join([node.text or '',*node.attrib.values()]).lower()
        if '@import' in css:return None
        for target in re.findall(r'url\(([^)]*)\)',css):
            if not target.strip().strip("\"'").startswith('#'):return None
    return 'svg'

class RunSource(Strict):
    source_id:str=Field(min_length=1,max_length=191)
    source_name:str=Field(min_length=1,max_length=255)
    batch_id:str|None=Field(default=None,pattern=r'^\d{20}-[0-9a-f]{32}$')
    collection:Collection

class ArchivedImage(Image):
    @model_validator(mode='after')
    def validate_file(self):
        if self.status == 'ok':
            if not self.path or '/images/' not in safe_path(self.path):
                raise ValueError('ok image requires an archive path')
        elif self.path is not None:
            raise ValueError('unavailable image must have null path')
        return self


class DailyArticle(Strict):
    id: str = Field(min_length=1, max_length=191)
    title: str = Field(min_length=1)
    authors: list[str]
    published_at: str
    url: str = Field(pattern=r'^https?://')
    content_state: Literal['ready', 'preview', 'no_text']
    text_path: str | None
    images: list[ArchivedImage]
    _time = field_validator('published_at')(valid_time)

    @model_validator(mode='after')
    def validate_paths(self):
        if (self.text_path is None) != (self.content_state == 'no_text'):
            raise ValueError('text path does not match content state')
        if self.text_path is not None and not safe_path(self.text_path).endswith('.txt'):
            raise ValueError('invalid text path')
        if len({image.position for image in self.images}) != len(self.images):
            raise ValueError('duplicate image position')
        return self


class DailySource(Strict):
    source_id: str = Field(min_length=1, max_length=191)
    source_name: str = Field(min_length=1, max_length=255)
    directory: str = Field(pattern=r'^[A-Za-z0-9_~-]{1,191}$')
    collection: Collection
    files: list[File] = Field(max_length=50000)
    articles: list[DailyArticle]

    @model_validator(mode='after')
    def validate_files(self):
        paths = {item.path for item in self.files}
        referenced = set()
        for article in self.articles:
            if article.text_path:
                referenced.add(article.text_path)
            referenced.update(image.path for image in article.images if image.path)
        if (len(paths) != len(self.files) or paths != referenced
                or any(not path.startswith(self.directory + '/') for path in paths)):
            raise ValueError('invalid or unreferenced archive files')
        if sum(item.size for item in self.files) > MAX_BATCH_BYTES:
            raise ValueError('archive source too large')
        if len({article.id for article in self.articles}) != len(self.articles):
            raise ValueError('duplicate article')
        return self


class DailyResult(Strict):
    version: Literal[1] = 1
    target_date: str
    status: Literal['success', 'failed', 'interrupted']
    sources: list[DailySource] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def validate_sources(self):
        if (len({source.source_id for source in self.sources}) != len(self.sources)
                or len({source.directory for source in self.sources}) != len(self.sources)):
            raise ValueError('duplicate source or directory')
        return self

class RunReport(Strict):
    version:Literal[1]=1
    id:str=Field(pattern=r'^\d{20}-[0-9a-f]{32}$')
    job_id:str=Field(pattern=r'^[0-9a-f-]{36}$')
    target_date:str
    execution_date:str|None=None
    timezone:Literal['Asia/Shanghai']='Asia/Shanghai'
    started_at:str
    finished_at:str
    status:Literal['success','failed','interrupted']
    sources:list[RunSource]=Field(min_length=1,max_length=100)
    _time=field_validator('started_at','finished_at')(valid_time)
    @field_validator('target_date')
    @classmethod
    def target_day(cls,value):
        from datetime import date
        if date.fromisoformat(value).isoformat()!=value:raise ValueError('invalid target date')
        return value
    @model_validator(mode='after')
    def validate_report(self):
        from zoneinfo import ZoneInfo
        day=datetime.fromisoformat(self.started_at.replace("Z","+00:00")).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        if self.execution_date is None:self.execution_date=day
        elif self.execution_date!=day:raise ValueError('execution date does not match start time')
        ids=[x.source_id for x in self.sources]
        batch_ids=[x.batch_id for x in self.sources if x.batch_id]
        if len(set(ids))!=len(ids) or len(set(batch_ids))!=len(batch_ids):raise ValueError('duplicate source/batch')
        if datetime.fromisoformat(self.finished_at.replace("Z","+00:00"))<datetime.fromisoformat(self.started_at.replace("Z","+00:00")):raise ValueError('invalid report time order')
        if self.status=='success' and any(x.collection.status!='success' for x in self.sources):raise ValueError('inconsistent report status')
        if any(x.collection.status=='success' and not x.batch_id for x in self.sources):raise ValueError('successful source needs a batch')
        return self
