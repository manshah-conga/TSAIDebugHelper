"""
Remote MCP transport: serves the tools defined in mcp_server.py over
Streamable HTTP at POST /mcp, so a client needs only a URL and a token --
no Python, no script copy, no per-machine config file.

Why this exists
---------------
stdio MCP is a local-subprocess model: the client launches
`python mcp_server.py` on its own machine. That works for one developer and
falls apart for a team. Streamable HTTP moves the MCP server onto the VM
next to the web app, so Claude Desktop, Claude Code, and Copilot Studio all
point at the same endpoint.

Authentication
--------------
The token arrives on the request, not from the environment, which is what
makes one shared endpoint safe for many people: each request is executed as
whoever owns the token on it, with that user's role and org visibility.

Accepted, in priority order:
  1. Authorization: Bearer <token>   -- preferred; use this wherever possible.
  2. X-TS-Token: <token>             -- for clients that reserve Authorization.
  3. ?token=<token> in the query      -- last resort, for clients that cannot
     set headers at all. Query strings get written to proxy and access logs,
     so a token used this way should be treated as lower-trust: give it the
     `reader` role and a short TTL. Disable this entirely by setting
     TS_MCP_ALLOW_QUERY_TOKEN=0.

The token is verified here, at the edge, so a bad token fails the connection
cleanly instead of turning into eighteen tools that each return an error
string. It is verified again downstream by the API routes themselves -- this
check is for a good error message, not a substitute for real enforcement.
"""
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from . import auth

# mcp_server.py sits one level up (webapp/), which is the directory uvicorn is
# started from, so it imports as a top-level module.
from mcp_server import mcp, CURRENT_TOKEN

ALLOW_QUERY_TOKEN = os.environ.get("TS_MCP_ALLOW_QUERY_TOKEN", "1").strip() not in ("0", "false", "no")

# FastMCP builds its session manager lazily, inside streamable_http_app(), and
# raises if you touch `mcp.session_manager` before that. We drive the manager
# ourselves (see mcp_asgi_app below) rather than serving the Starlette app it
# returns, so call it once here purely for the side effect of construction.
mcp.streamable_http_app()


def _extract_token(scope) -> str:
    """Pull the API token off a raw ASGI scope. Header names in `scope` are
    lowercase bytes; values are bytes."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

    authz = headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()

    x_token = headers.get("x-ts-token", "").strip()
    if x_token:
        return x_token

    if ALLOW_QUERY_TOKEN:
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        vals = qs.get("token") or qs.get("api_token")
        if vals and vals[0].strip():
            return vals[0].strip()

    return ""


async def _send_json(send, status: int, body: dict, headers=None):
    payload = json.dumps(body).encode("utf-8")
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    for k, v in (headers or {}).items():
        raw.append((k.encode("latin-1"), v.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": payload})


MCP_PATH = "/" + os.environ.get("TS_MCP_PATH", "mcp").strip().strip("/")


async def mcp_asgi_app(scope, receive, send):
    """Pure-ASGI entry point for the MCP endpoint."""
    token = _extract_token(scope)
    if not token:
        await _send_json(
            send, 401,
            {"error": "missing_token",
             "detail": "Send an API token as 'Authorization: Bearer <token>'. "
                       "Create one in the web UI under 'API Tokens'."},
            {"WWW-Authenticate": 'Bearer realm="ts-debug-helper"'},
        )
        return

    if not auth.verify_token(token):
        await _send_json(
            send, 401,
            {"error": "invalid_token",
             "detail": "That API token is not valid -- it may have been revoked, expired, or "
                       "belong to a disabled account."},
            {"WWW-Authenticate": 'Bearer realm="ts-debug-helper", error="invalid_token"'},
        )
        return

    # Bind the token for the duration of this request. anyio task groups copy
    # the current context when they spawn, so the tool coroutines the session
    # manager runs below inherit this value -- and only this one, never a
    # concurrent caller's.
    reset = CURRENT_TOKEN.set(token)
    try:
        await mcp.session_manager.handle_request(scope, receive, send)
    finally:
        CURRENT_TOKEN.reset(reset)


class MCPTransportMiddleware:
    """Routes /mcp to the MCP transport before FastAPI's router ever sees it.

    This is middleware rather than `app.mount("/mcp", ...)` on purpose. A
    Starlette mount answers a request for the bare `/mcp` with a 307 redirect
    to `/mcp/`, and MCP clients POSTing JSON-RPC do not reliably re-issue the
    body against the new location -- the connection just fails. Intercepting
    above the router sidesteps that, and accepts `/mcp` and `/mcp/` alike.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").rstrip("/") == MCP_PATH.rstrip("/"):
            await mcp_asgi_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan(app):
    """Runs the Streamable HTTP session manager for the life of the process.
    `mcp.session_manager` raises if a request arrives before this has started,
    so it must be wired into the FastAPI app's lifespan."""
    async with mcp.session_manager.run():
        yield
