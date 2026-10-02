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
from . import activity

# mcp_server.py sits one level up (webapp/), which is the directory uvicorn is
# started from, so it imports as a top-level module.
from mcp_server import mcp, CURRENT_TOKEN, CURRENT_CHANNEL, CURRENT_CLIENT

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

# --- client identification for activity analytics (app/activity.py) -------
#
# Stateless HTTP means a tools/call request carries no handshake of its own:
# the client said who it was once, in `initialize`, and every later request is
# anonymous as far as the protocol goes. So the name from the handshake is
# remembered per token (in memory; a restart just falls back to User-Agent
# until the client next initializes), and the User-Agent header covers the
# gap. Only the NAME is kept -- never the request body.
_CLIENT_BY_TOKEN = {}
_CLIENT_CACHE_MAX = 2000
_SNIFF_LIMIT = 64 * 1024   # an initialize body is tiny; a normalize_log call is not


def _user_agent(scope) -> str:
    for k, v in scope.get("headers", []):
        if k.decode("latin-1").lower() == "user-agent":
            return v.decode("latin-1")[:60]
    return ""


def _initialize_params(body: bytes):
    """clientInfo + protocolVersion from an initialize request, or None."""
    try:
        msg = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    for m in (msg if isinstance(msg, list) else [msg]):
        if isinstance(m, dict) and m.get("method") == "initialize":
            params = m.get("params") or {}
            return params.get("clientInfo") or {}, params.get("protocolVersion")
    return None


def _remember_client(token_id, name):
    if len(_CLIENT_BY_TOKEN) >= _CLIENT_CACHE_MAX:
        _CLIENT_BY_TOKEN.clear()
    _CLIENT_BY_TOKEN[token_id] = name


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

    ident = auth.verify_token(token)
    if not ident:
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
    channel_reset = CURRENT_CHANNEL.set("mcp-remote")
    client_reset = CURRENT_CLIENT.set(_CLIENT_BY_TOKEN.get(ident.get("token_id")) or _user_agent(scope))

    body = bytearray()
    sniffing = [True]
    handshake = []          # (client name, version, protocol) once seen
    status = [None]

    async def status_send(message):
        if message.get("type") == "http.response.start":
            status[0] = message.get("status")
        await send(message)

    async def sniffing_receive():
        message = await receive()
        if sniffing[0] and message.get("type") == "http.request":
            body.extend(message.get("body", b"") or b"")
            if len(body) > _SNIFF_LIMIT:
                sniffing[0] = False
                body.clear()
            elif not message.get("more_body"):
                sniffing[0] = False
                try:
                    found = _initialize_params(bytes(body))
                    if found is not None:
                        info, protocol = found
                        name = str(info.get("name") or "").strip() or _user_agent(scope)
                        handshake.append((name, info.get("version"), protocol))
                except Exception:  # noqa: BLE001 - analytics never breaks the transport
                    pass
                body.clear()
        return message

    try:
        await mcp.session_manager.handle_request(scope, sniffing_receive, status_send)
        # Only a handshake the server accepted counts as a connection.
        if handshake and status[0] and status[0] < 400:
            name, version, protocol = handshake[0]
            try:
                _remember_client(ident.get("token_id"), f"{name} {version or ''}".strip())
                activity.record_mcp_connect(ident["username"], ident.get("role"), name, version, protocol)
            except Exception:  # noqa: BLE001
                pass
    finally:
        CURRENT_CLIENT.reset(client_reset)
        CURRENT_CHANNEL.reset(channel_reset)
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
