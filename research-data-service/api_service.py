#!/usr/bin/env python3
"""Read API and MCP only; no scheduler, dispatcher, collection routes or service lock."""
import bootstrap
import argparse
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from fastapi import FastAPI
from aris.mcp_bridge import install_read_services
from aris.observability import setup_logging


def create_app(*, reader=None, api_base_url=None, mcp_client_factory=None):
    if not os.getenv('CORPUS_READ_API_TOKEN') or not os.getenv('CORPUS_CURSOR_SECRET'):
        raise ValueError('读取服务必须配置 CORPUS_READ_API_TOKEN 和 CORPUS_CURSOR_SECRET')

    @asynccontextmanager
    async def lifespan(app):
        async with bridge.manager.run():
            yield

    app = FastAPI(title='Overseas Corpus Read API and MCP', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        # Liveness only: deliberately does not touch database or object storage.
        return {'status': 'ok', 'service': 'read-api-mcp'}

    base = api_base_url or os.getenv('CORPUS_DATA_API_BASE_URL') or (
        'http://127.0.0.1:' + os.getenv('CORPUS_READ_PORT', '8000'))
    bridge = install_read_services(app, reader=reader, api_base_url=base,
                                   client_factory=mcp_client_factory)
    return app


def main():
    parser = argparse.ArgumentParser(description='只读 API 与 MCP 服务，不运行采集任务')
    parser.add_argument('--host', default=os.getenv('CORPUS_READ_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('CORPUS_READ_PORT', '8000')))
    args = parser.parse_args()
    logger = setup_logging('api')
    external = logging.getLogger('uvicorn')
    external.handlers = logger.handlers[:]
    external.propagate = False
    external.setLevel(logging.INFO)
    import uvicorn
    uvicorn.run(create_app(api_base_url=os.getenv('CORPUS_DATA_API_BASE_URL') or
                           f'http://127.0.0.1:{args.port}'),
                host=args.host, port=args.port, workers=1, access_log=False, log_config=None)


if __name__ == '__main__':
    main()
