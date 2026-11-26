"""Versioned read API. Isolated auth and errors preserve legacy management routes."""
import logging
import secrets
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from pymysql import Error as MySQLError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from aris.observability import log_context
from aris.read_data import ArticleReader, ReadError, SearchQuery, invalid

log=logging.getLogger('corpus.read_api')


def request_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):
        return str(uuid.uuid4())


def error_response(error, rid):
    return JSONResponse({'error':{'code':error.code,'message':error.message,'request_id':rid}},
                        status_code=error.status,
                        headers={'WWW-Authenticate':'Bearer'} if error.status==401 else None)


def create_read_app(token, cursor_secret, reader=None):
    if len(token)<24:
        raise ValueError('CORPUS_READ_API_TOKEN 至少需要 24 字符')
    reader=reader or ArticleReader(cursor_secret)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

    @app.middleware('http')
    async def boundary(request, call_next):
        rid=request_id(request.headers.get('X-Request-ID'))
        request.state.request_id=rid
        started=time.monotonic()
        # Log a route name rather than a URL/query string, which may contain user input.
        route='unknown'
        path=request.scope.get('path','')
        if path.endswith('/sources'): route='list_sources'
        elif path.endswith('/articles'): route='search_articles'
        elif path.endswith('/images'): route='get_article_images'
        elif '/articles/' in path: route='get_article'
        with log_context(request_id=rid,interface=route):
            try:
                scheme,_,credential=request.headers.get('Authorization','').partition(' ')
                if scheme.lower()!='bearer' or not secrets.compare_digest(credential.encode(),token.encode()):
                    raise ReadError(401,'UNAUTHORIZED','需要有效的读取凭证')
                if request.method=='GET' and (request.headers.get('content-length','0')!='0'
                                              or request.headers.get('transfer-encoding')):
                    raise invalid('GET 接口不接收请求体')
                response=await call_next(request)
            except ReadError as error:
                response=error_response(error,rid)
            except MySQLError:
                log.exception('read_dependency_failed')
                response=error_response(ReadError(503,'SERVICE_UNAVAILABLE','数据服务暂不可用'),rid)
            except Exception:
                log.exception('read_request_failed')
                response=error_response(ReadError(500,'INTERNAL_ERROR','服务内部异常'),rid)
            response.headers['X-Request-ID']=rid
            response.headers['Cache-Control']='private, no-store'
            log.log(logging.ERROR if response.status_code>=500 else logging.INFO,
                    'read_finished status=%s duration_seconds=%.3f',response.status_code,time.monotonic()-started)
            return response

    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        code='NOT_FOUND' if error.status_code==404 else 'INVALID_ARGUMENT'
        return error_response(ReadError(error.status_code,code,'请求的接口不存在或方法不支持'),request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        return error_response(invalid(),request.state.request_id)

    def query_params(request,allowed):
        if set(request.query_params)-set(allowed):
            raise invalid('存在未声明的查询参数')
        result={}
        for name in request.query_params:
            values=request.query_params.getlist(name)
            if name=='source_id':
                result[name]=values
            elif len(values)!=1:
                raise invalid('标量查询参数不可重复')
            else:
                result[name]=values[0]
        return result

    def envelope(request,data):
        return {'data':data,'request_id':request.state.request_id}

    @app.get('/sources')
    def list_sources(request:Request):
        query_params(request,[])
        return envelope(request,reader.list_sources())

    @app.get('/articles')
    def search_articles(request:Request):
        values=query_params(request,SearchQuery.model_fields)
        if 'page_size' in values:
            if not values['page_size'].isascii() or not values['page_size'].isdigit():
                raise invalid('page_size 必须是整数')
            values['page_size']=int(values['page_size'])
        if 'merge_duplicates' in values:
            if values['merge_duplicates'] not in ('true', 'false'):
                raise invalid('merge_duplicates 必须为 true 或 false')
            values['merge_duplicates'] = values['merge_duplicates'] == 'true'
        try:
            query=SearchQuery.model_validate(values)
        except ValidationError:
            raise invalid('查询参数、时间区间或分页大小不合法') from None
        return envelope(request,reader.search_articles(query))

    @app.get('/articles/{article_id}')
    def get_article(request:Request, article_id:str):
        query_params(request,[])
        with log_context(doc_id=article_id):
            return envelope(request,reader.get_article(article_id))

    @app.get('/articles/{article_id}/images')
    def get_article_images(request:Request, article_id:str):
        query_params(request,[])
        with log_context(doc_id=article_id):
            return envelope(request,reader.get_article_images(article_id))

    return app
