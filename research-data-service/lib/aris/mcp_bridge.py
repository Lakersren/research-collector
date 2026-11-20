"""Official MCP Streamable HTTP transport; tools call the fixed read API only."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import quote, urlsplit

import httpx
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool
from starlette.responses import JSONResponse

from aris.observability import log_context
from aris.read_api import request_id

log=logging.getLogger('corpus.mcp')
_token=ContextVar('corpus_mcp_read_token')
_rid=ContextVar('corpus_mcp_request_id')


class MCPBridge:
    def __init__(self, token, api_base_url, client_factory=None):
        if len(token)<24:
            raise ValueError('CORPUS_READ_API_TOKEN 至少需要 24 字符')
        parsed=urlsplit(api_base_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password \
           or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError('CORPUS_DATA_API_BASE_URL 必须是固定的 API 根地址')
        self.token,self.base=token,api_base_url.rstrip('/')
        self.client_factory=client_factory or (lambda: httpx.AsyncClient(timeout=30,trust_env=False,follow_redirects=False))
        from aris.paths import CONTRACTS
        definition_path=CONTRACTS/'mcp-tools.json'
        self.tools={item['name']:Tool.model_validate(item) for item in json.loads(definition_path.read_text())['tools']}
        self.server=Server('overseas-corpus',version='1.0.0',instructions='只读已采集语料；全文与预览须区分，保留原文引用。')

        @self.server.list_tools()
        async def list_tools():
            return list(self.tools.values())

        @self.server.call_tool()
        async def call_tool(name, arguments):
            return await self.call(name,arguments,_token.get(),_rid.get())

        hosts=os.getenv('CORPUS_MCP_ALLOWED_HOSTS','127.0.0.1:*,localhost:*,[::1]:*').split(',')
        origins=os.getenv('CORPUS_MCP_ALLOWED_ORIGINS','http://127.0.0.1:*,http://localhost:*').split(',')
        security=TransportSecuritySettings(allowed_hosts=[x.strip() for x in hosts if x.strip()],
                                            allowed_origins=[x.strip() for x in origins if x.strip()])
        self.manager=StreamableHTTPSessionManager(self.server,json_response=True,stateless=True,security_settings=security)

    async def __call__(self, scope, receive, send):
        headers={k.decode().lower():v.decode() for k,v in scope.get('headers',[])}
        rid=request_id(headers.get('x-request-id'))
        scheme,_,token=headers.get('authorization','').partition(' ')
        if scheme.lower()!='bearer' or not secrets.compare_digest(token.encode(),self.token.encode()):
            response=JSONResponse({'error':{'code':'UNAUTHORIZED','message':'需要有效的读取凭证','request_id':rid}},
                                  status_code=401,headers={'WWW-Authenticate':'Bearer','X-Request-ID':rid,
                                                           'Cache-Control':'private, no-store'})
            await response(scope,receive,send)
            return
        t1,t2=_token.set(token),_rid.set(rid)
        started=time.monotonic()
        status=500
        async def traced_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status']
                message.setdefault('headers',[]).extend([(b'x-request-id',rid.encode()),
                                                        (b'cache-control',b'private, no-store')])
            await send(message)
        try:
            with log_context(request_id=rid,interface='mcp'):
                await self.manager.handle_request(scope,receive,traced_send)
                log.info('mcp_http_finished status=%s duration_seconds=%.3f',status,time.monotonic()-started)
        finally:
            _token.reset(t1)
            _rid.reset(t2)

    @staticmethod
    def error(code,message,rid):
        value={'error':{'code':code,'message':message,'request_id':rid}}
        return CallToolResult(isError=True,content=[TextContent(type='text',text=json.dumps(value,ensure_ascii=False))])

    async def call(self,name,arguments,token,rid):
        with log_context(request_id=rid,interface='mcp',tool=name):
            if name not in self.tools:
                return self.error('INVALID_ARGUMENT','工具不存在',rid)
            if name=='list_sources':
                path,params='/v1/sources',None
            elif name=='search_articles':
                path='/v1/articles'
                params=[(key,item) for key,value in arguments.items()
                        for item in (value if isinstance(value,list) else [value])]
            else:
                article_id=quote(arguments['id'],safe='')
                path='/v1/articles/'+article_id+('/images' if name=='get_article_images' else '')
                params=None
            try:
                async with self.client_factory() as client:
                    response=await client.get(self.base+path,params=params,
                        headers={'Authorization':'Bearer '+token,'X-Request-ID':rid})
                if response.status_code!=200:
                    # Only propagate a well-formed public error; never echo proxy HTML or SDK exceptions.
                    try:
                        row=response.json()['error']
                        codes={'UNAUTHORIZED','FORBIDDEN','NOT_FOUND','INVALID_ARGUMENT','INTERNAL_ERROR','SERVICE_UNAVAILABLE'}
                        if set(row)!={'code','message','request_id'} or row['code'] not in codes:
                            raise ValueError()
                        # Error messages from this fixed API are part of the public contract.
                        if not isinstance(row['message'],str) or not isinstance(row['request_id'],str):
                            raise ValueError()
                        return self.error(row['code'],row['message'],row['request_id'])
                    except (ValueError,KeyError,TypeError):
                        log.error('mcp_upstream_error status=%s',response.status_code)
                        return self.error('SERVICE_UNAVAILABLE','读取 API 暂不可用',rid)
                value=response.json()
                validator=Draft202012Validator(self.tools[name].outputSchema,format_checker=Draft202012Validator.FORMAT_CHECKER)
                if not validator.is_valid(value):
                    log.error('mcp_upstream_schema_mismatch')
                    return self.error('INTERNAL_ERROR','读取 API 返回格式不符合契约',rid)
                log.info('mcp_tool_finished status=success')
                # Official SDK produces matching text + structuredContent and validates outputSchema.
                return value
            except httpx.RequestError:
                log.error('mcp_upstream_unavailable')
                return self.error('SERVICE_UNAVAILABLE','读取 API 暂不可用',rid)
            except Exception:
                # Avoid putting arbitrary upstream data or signed image URLs in logs/errors.
                log.error('mcp_tool_internal_error')
                return self.error('INTERNAL_ERROR','工具执行失败',rid)


def install_read_services(app, *, reader=None, api_base_url=None, client_factory=None):
    """Install read routes into the dedicated API application."""
    from aris.read_api import create_read_app
    from starlette.routing import Route
    token=os.getenv('CORPUS_READ_API_TOKEN','')
    cursor=os.getenv('CORPUS_CURSOR_SECRET','')
    if not token and not cursor:
        return None
    if len(token)<24 or len(cursor)<32 or token==os.getenv('CORPUS_API_TOKEN') or token==cursor:
        raise ValueError('读取令牌和游标签名密钥须独立配置，长度分别至少为 24 和 32 字符')
    base=api_base_url or os.getenv('CORPUS_DATA_API_BASE_URL') or 'http://127.0.0.1:'+os.getenv('CORPUS_READ_PORT','8000')
    bridge=MCPBridge(token,base,client_factory)
    app.mount('/v1',create_read_app(token,cursor,reader=reader))
    app.router.routes.append(Route('/mcp',endpoint=bridge,methods=['GET','POST','DELETE']))
    return bridge
