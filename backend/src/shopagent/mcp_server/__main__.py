"""Run the MCP server over stdio, signed in as the customer named by the environment.

    SHOPAGENT_MCP_EMAIL=... SHOPAGENT_MCP_PASSWORD=... uv run --extra rerank python -m shopagent.mcp_server

The database is the one in DATABASE_URL (.env), as for the API. Logs go to stderr: stdout carries the protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp.server.stdio import stdio_server

from shopagent.infra.db.session import get_sessionmaker
from shopagent.mcp_server.server import (
    AuthenticationError,
    authenticate_customer,
    build_server,
    open_conversation,
)

logger = logging.getLogger("shopagent.mcp_server")


async def serve() -> None:
    email = os.environ.get("SHOPAGENT_MCP_EMAIL", "")
    password = os.environ.get("SHOPAGENT_MCP_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Set SHOPAGENT_MCP_EMAIL and SHOPAGENT_MCP_PASSWORD to a customer account.")
    factory = get_sessionmaker()
    try:
        principal = await authenticate_customer(factory, email, password)
    except AuthenticationError as e:
        raise SystemExit(str(e)) from None
    conversation_id = await open_conversation(factory, principal)
    logger.info("mcp_server_ready conversation_id=%s", conversation_id)
    server = build_server(factory, principal, conversation_id)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    if sys.platform == "win32":
        # psycopg's async driver cannot run on the Windows ProactorEventLoop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(serve())


if __name__ == "__main__":
    main()
