"""Read-only corpus queries, signed cursors and image URL presentation."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import pymysql

from aris.kb import connect
from aris.source_config import sources


class ReadError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def invalid(message='请求参数不合法'):
    return ReadError(422, 'INVALID_ARGUMENT', message)


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value
    ):
        raise ValueError('时间必须是带时区的 RFC 3339 时间')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    q: str | None = Field(default=None, min_length=1, max_length=200)
    source_id: list[str] | None = Field(default=None, min_length=1, max_length=29)
    content_state: Literal['ready', 'preview', 'no_text'] | None = None
    published_from: str | None = None
    published_to: str | None = None
    ingested_from: str | None = None
    ingested_to: str | None = None
    merge_duplicates: bool = False
    industry: str | None = Field(default=None, min_length=1, max_length=128)
    theme: str | None = Field(default=None, min_length=1, max_length=128)
    sort: Literal['published_at', 'ingested_at'] = 'published_at'
    page_size: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator('q', mode='before')
    @classmethod
    def strip_query(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator('source_id')
    @classmethod
    def valid_sources(cls, value):
        if value is not None:
            if len(value) != len(set(value)) or any(not x or len(x)>191 for x in value):
                raise ValueError('来源不能重复且 ID 必须有效')
            return sorted(value)
        return value

    @field_validator('published_from', 'published_to', 'ingested_from', 'ingested_to')
    @classmethod
    def normalize_time(cls, value):
        return iso(parse_time(value)) if value is not None else None

    @model_validator(mode='after')
    def validate_ranges(self):
        for prefix in ('published', 'ingested'):
            start, end = getattr(self, prefix+'_from'), getattr(self, prefix+'_to')
            if start is not None and end is not None and parse_time(start) >= parse_time(end):
                raise ValueError(prefix+'_from 必须早于 '+prefix+'_to')
        return self


class CursorCodec:
    def __init__(self, secret, clock=utcnow):
        if len(secret) < 32:
            raise ValueError('CORPUS_CURSOR_SECRET 至少需要 32 字符')
        self.secret, self.clock = secret.encode(), clock

    @staticmethod
    def fingerprint(query):
        canonical = json.dumps(query, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=').decode()

    def encode(self, fingerprint, as_of, expires_at, last_time, last_id):
        row = dict(v=1, fingerprint=fingerprint, as_of=iso(as_of), expires_at=expires_at,
                   last_time=iso(last_time), last_id=last_id)
        payload = self._encode(json.dumps(row, separators=(',', ':')).encode())
        signature = self._encode(hmac.digest(self.secret, payload.encode(), 'sha256'))
        return payload+'.'+signature

    def decode(self, token, fingerprint, sort):
        try:
            if len(token)>2048 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
                raise ValueError()
            payload, signature = token.split('.')
            expected = self._encode(hmac.digest(self.secret, payload.encode(), 'sha256'))
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            row = json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            if set(row) != {'v','fingerprint','as_of','expires_at','last_time','last_id'}:
                raise ValueError()
            if row['v'] != 1 or row['fingerprint'] != fingerprint:
                raise ValueError()
            if type(row['expires_at']) is not int or row['expires_at'] <= self.clock().timestamp():
                raise ValueError()
            if not isinstance(row['last_id'], str) or not 1 <= len(row['last_id']) <= 191:
                raise ValueError()
            row['as_of'] = parse_time(row['as_of'])
            row['last_time'] = parse_time(row['last_time']) if row['last_time'] is not None else None
            if sort=='ingested_at' and row['last_time'] is None:
                raise ValueError()
            return row
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise invalid('游标无效、已过期或与查询条件不匹配') from None


@contextmanager
def read_session(connection_factory=connect):
    conn = connection_factory()
    try:
        # Transaction-level protection in addition to SELECT-only repository methods.
        with conn.cursor() as cursor:
            cursor.execute('START TRANSACTION READ ONLY')
        yield ReadRepository(conn)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


class ReadRepository:
    def __init__(self, connection):
        self.connection = connection

    def _query(self, sql, params=()):
        with self.connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def source_stats(self):
        return {row['id']:row for row in self._query('SELECT id,n_docs,first_ts,last_ts FROM `source`')}

    def search(self, query, as_of, last):
        source_ids = query['source_id']
        conditions = ['d.source_id IN ('+','.join(['%s']*len(source_ids))+')', 'd.ingested_at <= %s']
        params = [*source_ids, as_of.replace(tzinfo=None)]
        if query['q'] is not None:
            conditions.append("d.title COLLATE utf8mb4_0900_as_ci LIKE %s ESCAPE '!'")
            escaped = query['q'].replace('!', '!!').replace('%', '!%').replace('_', '!_')
            params.append('%'+escaped+'%')
        if query['content_state'] is not None:
            conditions.append('d.content_state=%s')
            params.append(query['content_state'])
        for column in ('published_at', 'ingested_at'):
            prefix = column.removesuffix('_at')
            for suffix, operator in (('from','>='),('to','<')):
                value = query[prefix+'_'+suffix]
                if value is not None:
                    conditions.append(f'd.{column} {operator} %s')
                    params.append(parse_time(value).replace(tzinfo=None))
        for label_type in ('industry', 'theme'):
            if query.get(label_type) is not None:
                conditions.append(
                    'EXISTS (SELECT 1 FROM doc_label l WHERE l.doc_id=d.id '
                    'AND l.label_type=%s AND l.label_value=%s)'
                )
                params.extend((label_type, query[label_type]))
        # sort is validated before reaching SQL and still explicitly checked here.
        sort = query['sort']
        if sort not in ('published_at','ingested_at'):
            raise invalid('排序字段无效')
        prefix = ''
        table = '`doc` d'
        group_columns = ''
        if query.get('merge_duplicates'):
            # Group the filtered result first, then apply the cursor to representative rows.
            prefix = (
                f'WITH matched AS (SELECT d.id,d.content_hash,d.source_id,d.{sort} FROM doc d WHERE '
                + ' AND '.join(conditions)
                + f'), ranked AS (SELECT matched.*, ROW_NUMBER() OVER (PARTITION BY content_hash, '
                  f'CASE WHEN content_hash IS NULL THEN id END ORDER BY {sort} DESC, CAST(id AS BINARY) DESC) AS rn '
                  f'FROM matched), group_sources AS (SELECT content_hash, COUNT(DISTINCT source_id) AS source_count '
                  f'FROM matched WHERE content_hash IS NOT NULL GROUP BY content_hash) '
            )
            table += (' JOIN ranked r ON r.id=d.id '
                      'LEFT JOIN group_sources g ON g.content_hash=r.content_hash')
            group_columns = ',COALESCE(g.source_count,1) AS source_count'
            conditions = ['r.rn=1']
        if last:
            if last['last_time'] is None:
                conditions.append(f'd.{sort} IS NULL AND CAST(d.id AS BINARY)<CAST(%s AS BINARY)')
                params.append(last['last_id'])
            else:
                predicate = f'(d.{sort}<%s OR (d.{sort}=%s AND CAST(d.id AS BINARY)<CAST(%s AS BINARY))'
                if sort=='published_at':
                    predicate += f' OR d.{sort} IS NULL'
                conditions.append(predicate+')')
                when = last['last_time'].replace(tzinfo=None)
                params.extend([when,when,last['last_id']])
        params.append(query['page_size']+1)
        return self._query(
            prefix + "SELECT d.id,d.source_id,d.title,d.authors,d.published_at,d.ingested_at,d.url,d.content_state,"
            "CASE WHEN d.content_state='no_text' THEN NULL ELSE "
            "LEFT(TRIM(REGEXP_REPLACE(d.content,'[[:space:]]+',' ')),300) END AS summary"
            + group_columns + ' FROM ' + table + ' WHERE ' + ' AND '.join(conditions) +
            f' ORDER BY d.{sort} DESC,CAST(d.id AS BINARY) DESC LIMIT %s', tuple(params))

    def labels(self, article_ids):
        if not article_ids:
            return {}
        placeholders = ','.join(['%s'] * len(article_ids))
        rows = self._query(
            f'SELECT doc_id,label_type,label_value,is_primary FROM doc_label '
            f'WHERE doc_id IN ({placeholders}) ORDER BY label_type,label_value',
            article_ids,
        )
        labels = {}
        for row in rows:
            labels.setdefault(row['doc_id'], []).append(
                dict(label_type=row['label_type'], label_value=row['label_value'],
                     is_primary=bool(row['is_primary']))
            )
        return labels

    def article(self, article_id):
        rows = self._query('SELECT id,source_id,title,authors,published_at,ingested_at,url,content_state,content '
                           'FROM `doc` WHERE id=%s', (article_id,))
        return rows[0] if rows else None

    def images(self, article_id):
        return self._query('SELECT position,file_key,status,width,height FROM `doc_image` '
                           'WHERE doc_id=%s ORDER BY position', (article_id,))


class ImageSigner:
    def __init__(self):
        self.client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.client is not None:
            self.client.close()

    def sign(self, key):
        try:
            if self.client is None:
                required = ['XSKY_ENDPOINT','XSKY_BUCKET','XSKY_ACCESS_KEY','XSKY_SECRET_KEY']
                if any(not os.getenv(name) for name in required):
                    raise ReadError(503,'SERVICE_UNAVAILABLE','图片访问服务暂不可用')
                endpoint = os.getenv('CORPUS_IMAGE_PUBLIC_ENDPOINT') or os.environ['XSKY_ENDPOINT']
                self.client = boto3.client('s3', endpoint_url=endpoint,
                    aws_access_key_id=os.environ['XSKY_ACCESS_KEY'], aws_secret_access_key=os.environ['XSKY_SECRET_KEY'],
                    aws_session_token=os.getenv('XSKY_SESSION_TOKEN'), region_name=os.getenv('XSKY_REGION','us-east-1'),
                    verify=os.getenv('XSKY_CA_BUNDLE') or True,
                    config=Config(signature_version='s3v4',proxies={},connect_timeout=10,read_timeout=15,
                                  s3={'addressing_style':os.getenv('XSKY_ADDRESSING_STYLE','path')}))
            url = self.client.generate_presigned_url('get_object',
                Params={'Bucket':os.environ['XSKY_BUCKET'],'Key':key}, ExpiresIn=900)
            signed_at = datetime.strptime(parse_qs(urlsplit(url).query)['X-Amz-Date'][0], '%Y%m%dT%H%M%SZ')
            return url, iso(signed_at.replace(tzinfo=timezone.utc)+timedelta(seconds=900))
        except (BotoCoreError, ClientError, ValueError):
            raise ReadError(503,'SERVICE_UNAVAILABLE','图片访问服务暂不可用') from None


class ArticleReader:
    def __init__(self, cursor_secret, session_factory=read_session, source_factory=sources,
                 signer_factory=ImageSigner, clock=utcnow):
        self.source_map = {s['key']:s for s in source_factory()}
        self.sessions, self.signers, self.clock = session_factory, signer_factory, clock
        self.cursors = CursorCodec(cursor_secret, clock)

    def list_sources(self):
        with self.sessions() as repo:
            stats = repo.source_stats()
        return {'items':[dict(id=key,name=s['name'],channel='web',n_docs=stats.get(key,{}).get('n_docs',0),
                              first_ts=iso(stats.get(key,{}).get('first_ts')),
                              last_ts=iso(stats.get(key,{}).get('last_ts')))
                         for key,s in sorted(self.source_map.items())]}

    def search_articles(self, query):
        normalized = query.model_dump(exclude={'cursor'})
        for field in ('industry', 'theme', 'merge_duplicates'):
            if not normalized[field]:
                del normalized[field]
        normalized['source_id'] = query.source_id or sorted(self.source_map)
        if any(key not in self.source_map for key in normalized['source_id']):
            raise invalid('存在未登记的来源 ID')
        fingerprint = self.cursors.fingerprint(normalized)
        last = self.cursors.decode(query.cursor, fingerprint, query.sort) if query.cursor else None
        as_of = last['as_of'] if last else self.clock()
        expires_at = last['expires_at'] if last else int(as_of.timestamp())+86400
        with self.sessions() as repo:
            rows = repo.search(normalized, as_of, last)
            more = len(rows) > query.page_size
            rows = rows[:query.page_size]
            labels = repo.labels([row['id'] for row in rows])
        next_cursor = None
        if more:
            tail = rows[-1]
            next_cursor = self.cursors.encode(fingerprint,as_of,expires_at,tail[query.sort],tail['id'])
        return {'items':[self._present(row, labels.get(row['id'])) for row in rows],
                'page':{'next_cursor':next_cursor,'has_more':more}}

    def _present(self, row, labels=None):
        result = dict(row)
        if labels:
            result['labels'] = labels
        result['source_name'] = self.source_map[result['source_id']]['name']
        authors = result['authors']
        result['authors'] = json.loads(authors) if isinstance(authors,(str,bytes)) else authors
        if not isinstance(result['authors'],list) or any(not isinstance(a,str) for a in result['authors']):
            raise RuntimeError('invalid authors in stored article')
        result['published_at'], result['ingested_at'] = iso(result['published_at']),iso(result['ingested_at'])
        if result['content_state']=='no_text' and 'content' in result:
            result['content']=None
        return result

    def _get(self, repo, article_id):
        if not 1<=len(article_id)<=191:
            raise invalid('文章 ID 长度无效')
        row = repo.article(article_id)
        if row is None or row['source_id'] not in self.source_map:
            raise ReadError(404,'NOT_FOUND','文章不存在')
        return row

    def _images(self, rows):
        result=[]
        with self.signers() as signer:
            for row in rows:
                item={key:row[key] for key in ('position','status','width','height')}
                item.update(url=None,expires_at=None)
                if row['status']=='ok':
                    if not row['file_key']:
                        raise RuntimeError('ok image has no file_key')
                    item['url'], item['expires_at'] = signer.sign(row['file_key'])
                elif row['status'] not in ('pending','failed'):
                    raise RuntimeError('invalid stored image state')
                result.append(item)
        return result

    def get_article(self, article_id):
        with self.sessions() as repo:
            row = self._get(repo,article_id)
            images = repo.images(article_id)
            labels = repo.labels([article_id]).get(article_id)
        return {**self._present(row, labels),'images':self._images(images)}

    def get_article_images(self, article_id):
        with self.sessions() as repo:
            self._get(repo,article_id)
            images = repo.images(article_id)
        return {'article_id':article_id,'items':self._images(images)}
