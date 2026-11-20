#!/usr/bin/env python3
"""Standalone MCP adapter: configure read token and API URL, no DB/S3 credentials needed."""
import bootstrap
import argparse
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'lib'))
from starlette.applications import Starlette
from starlette.routing import Route
import uvicorn
from aris.mcp_bridge import MCPBridge
from aris.observability import setup_logging


def create_app(client_factory=None):
    bridge=MCPBridge(os.getenv('CORPUS_READ_API_TOKEN',''),os.getenv('CORPUS_DATA_API_BASE_URL',''),client_factory)
    @asynccontextmanager
    async def lifespan(app):
        async with bridge.manager.run():
            yield
    return Starlette(routes=[Route('/mcp',endpoint=bridge,methods=['GET','POST','DELETE'])],lifespan=lifespan)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='独立只读 MCP 服务')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8001)
    args=parser.parse_args()
    setup_logging('mcp')
    uvicorn.run(create_app(),host=args.host,port=args.port,access_log=False,log_config=None)
