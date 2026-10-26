"""
Congaroo: Conga's own product knowledge as a second, optional tool source
for the in-app chat.

What it is for
--------------
Everything else the chat model can call describes ONE customer org's
customization. That answers "what in this org writes the field", but not
"is this how Conga CPQ behaves out of the box", "is there a JIRA for this
error", or "has another customer hit this". Congaroo -- Conga's internal
knowledgebase (KB articles, documentation, support cases, JIRAs), exposed by
Workato as a remote MCP server -- answers exactly those. With both, an RCA
can separate "the customer's trigger did this" from "the managed package
does this by design / has a known defect".

It is per user and off by default: a user turns it on in Chat settings and
signs in to Congaroo once with their own Conga identity, so every search
runs with that person's own Congaroo permissions -- never a shared account.

The OAuth flow (MCP authorization spec, 2025-06-18)
---------------------------------------------------
Nothing about the authorization server is hard-coded; it is discovered:

  1. GET <mcp origin>/.well-known/oauth-protected-resource
       -> `authorization_servers[0]` (the issuer) and `resource`.
     (Fallback: the `resource_metadata=` hint in a 401's WWW-Authenticate.)
  2. GET the issuer's RFC 8414 / OIDC metadata
       -> authorize, token, registration and revocation endpoints.
  3. Dynamic client registration (RFC 7591) as a PUBLIC client
     (token_endpoint_auth_method "none"), once per redirect URI, cached in
     data/auth/congaroo_clients.json. A client_id is not a secret.
     TS_CONGAROO_CLIENT_ID skips registration for a pre-registered client.
  4. Authorization code + PKCE S256, with `resource` (RFC 8707) so the
     token is minted for the Congaroo MCP server and nothing else.
  5. Refresh-token rotation; a refused refresh means "reconnect", said
     plainly in the chat rather than as a 500.

Where the tokens live
---------------------
In this process's memory by default -- like the personal LLM keys in
secrets_store.py, a server restart means each user clicks Connect again
(normally one click: Workato remembers the SSO session). Set
TS_CONGAROO_TOKEN_KEY to a long random value to persist them instead,
AES-GCM encrypted under that key, username as associated data, in
data/auth/congaroo_tokens.json. The key must come from the environment and
is never written next to the data it protects.

The MCP client
--------------
A deliberately small Streamable HTTP client (initialize ->
notifications/initialized -> tools/list | tools/call) over httpx, built with
sf_client.make_async_client so it gets the same IPv4-only transport and
Windows proxy detection that fixed the Salesforce timeouts. Responses may be
plain JSON or a one-shot SSE stream; both are handled. An expired MCP
session (404) re-initializes; a 401 refreshes the token and retries once.
"""
import asyncio
import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import storage

DEFAULT_URL = "https://16406.apim.mcp.workato.com/"
TOOL_PREFIX = "congaroo_"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "TS Intelligent Debug Helper"
CALLBACK_PATH = "/api/congaroo/oauth/callback"

HTTP_TIMEOUT = float(os.environ.get("TS_CONGAROO_TIMEOUT", "60"))
PENDING_TTL = 600            # an unfinished sign-in is forgotten after 10 minutes
DISCOVERY_TTL = 3600
TOOLS_TTL = 600
METADATA_TTL = 1800          # Congaroo's "fetch metadata" result: filter values, slow to change
REFRESH_SKEW = 60            # refresh this many seconds before expiry


class CongarooError(Exception):
    def __init__(self, message, code="congaroo_error"):
        super().__init__(message)
        self.code = code


class NotConnected(CongarooError):
    def __init__(self, message="Congaroo is not connected for this user. Connect it from Chat "
                               "settings (the gear icon in the chat panel).", code="congaroo_not_connected"):
        super().__init__(message, code)


class _Unauthorized(Exception):
    """Internal: the MCP server answered 401 to a token we thought was good."""


# ---------- configuration ----------

def server_url():
    return (os.environ.get("TS_CONGAROO_URL") or DEFAULT_URL).strip()


def available():
    """Server-wide switch. On unless an admin turns it off."""
    return os.environ.get("TS_CONGAROO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def redirect_uri(request_base_url=None):
    """Where Workato sends the browser back. Explicit override first, then the
    app's public URL, then whatever host the browser used to reach us -- the
    last is right for localhost and for a directly-exposed VM, wrong behind a
    TLS-terminating proxy, which is what the first two are for."""
    explicit = (os.environ.get("TS_CONGAROO_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    public = (os.environ.get("TS_PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public + CALLBACK_PATH
    base = (request_base_url or "http://127.0.0.1:8000/").rstrip("/")
    return base + CALLBACK_PATH


def _clients_path():
    return os.path.join(storage.AUTH_ROOT, "congaroo_clients.json")


def _tokens_path():
    return os.path.join(storage.AUTH_ROOT, "congaroo_tokens.json")


def _http():
    try:
        from .sf_client import make_async_client
        return make_async_client(server_url(), timeout=HTTP_TIMEOUT)
    except Exception:                                 # noqa: BLE001 - fall back to a plain client
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT)


def _now():
    return time.time()


def _iso(ts=None):
    return datetime.datetime.fromtimestamp(ts if ts is not None else _now(),
                                           datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------- discovery ----------

_DISCOVERY = {"at": 0, "value": None}


async def _get_json(client, url):
    try:
        r = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        j = r.json()
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def _split(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}", p.path.rstrip("/")


async def discover(client, force=False):
    """Protected-resource + authorization-server metadata, cached an hour."""
    if not force and _DISCOVERY["value"] and _now() - _DISCOVERY["at"] < DISCOVERY_TTL:
        return _DISCOVERY["value"]
    url = server_url()
    origin, path = _split(url)
    prm = None
    candidates = []
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    for c in candidates:
        j = await _get_json(client, c)
        if j and j.get("authorization_servers"):
            prm = j
            break
    if prm is None:
        # The spec's other route: an unauthenticated request answers 401 with
        # a pointer to the metadata document.
        try:
            r = await client.post(url, json=_init_payload(0),
                                  headers={"Accept": "application/json, text/event-stream"})
            m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", ""))
            if m:
                j = await _get_json(client, m.group(1))
                if j and j.get("authorization_servers"):
                    prm = j
        except httpx.HTTPError:
            pass
    if prm is None:
        raise CongarooError(f"Could not find OAuth metadata for {url}. Check TS_CONGAROO_URL and "
                            f"that this server can reach it.", "congaroo_discovery")

    issuer = str(prm["authorization_servers"][0]).rstrip("/")
    i_origin, i_path = _split(issuer)
    as_meta = None
    for c in (f"{i_origin}/.well-known/oauth-authorization-server{i_path}",
              f"{i_origin}/.well-known/openid-configuration{i_path}",
              f"{issuer}/.well-known/openid-configuration",
              f"{issuer}/.well-known/oauth-authorization-server"):
        j = await _get_json(client, c)
        if j and j.get("authorization_endpoint") and j.get("token_endpoint"):
            as_meta = j
            break
    if as_meta is None:
        raise CongarooError(f"Could not read the authorization server metadata at {issuer}.",
                            "congaroo_discovery")
    methods = as_meta.get("code_challenge_methods_supported")
    if methods and "S256" not in methods:
        raise CongarooError("Congaroo's sign-in server does not support PKCE S256.", "congaroo_discovery")
    value = {"prm": prm, "as": as_meta, "issuer": issuer,
             "resource": prm.get("resource") or url.rstrip("/")}
    _DISCOVERY.update(at=_now(), value=value)
    return value


def _scope(disc):
    explicit = (os.environ.get("TS_CONGAROO_SCOPES") or "").strip()
    if explicit:
        return explicit
    scopes = disc["prm"].get("scopes_supported") or disc["as"].get("scopes_supported") or []
    return " ".join(str(s) for s in scopes)


# ---------- client registration ----------

async def _client_registration(client, disc, redirect):
    env_id = (os.environ.get("TS_CONGAROO_CLIENT_ID") or "").strip()
    if env_id:
        secret = (os.environ.get("TS_CONGAROO_CLIENT_SECRET") or "").strip() or None
        return {"client_id": env_id, "client_secret": secret,
                "auth_method": "client_secret_post" if secret else "none"}
    key = f"{disc['issuer']}|{redirect}"
    existing = (storage.read_json(_clients_path(), {}) or {}).get(key)
    if existing and existing.get("client_id"):
        return existing
    reg_url = disc["as"].get("registration_endpoint")
    if not reg_url:
        raise CongarooError("Congaroo's sign-in server does not allow dynamic client registration. "
                            "Ask for a client id and set TS_CONGAROO_CLIENT_ID.", "congaroo_registration")
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    scope = _scope(disc)
    if scope:
        body["scope"] = scope
    try:
        r = await client.post(reg_url, json=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise CongarooError(f"Client registration with Congaroo failed: {e.__class__.__name__}.",
                            "congaroo_registration")
    if r.status_code not in (200, 201):
        raise CongarooError(f"Congaroo refused to register this app (HTTP {r.status_code}): "
                            f"{r.text[:300]}", "congaroo_registration")
    j = r.json()
    rec = {"client_id": j.get("client_id"), "client_secret": j.get("client_secret"),
           "auth_method": j.get("token_endpoint_auth_method") or "none",
           "redirect_uri": redirect, "registered_at": _iso()}
    if not rec["client_id"]:
        raise CongarooError("Congaroo's registration response had no client_id.", "congaroo_registration")
    storage.mutate_json(_clients_path(), lambda d: d.__setitem__(key, rec), {})
    return rec


def _client_auth(reg, data):
    """Apply the registered client authentication to a token request."""
    method = reg.get("auth_method") or "none"
    secret = reg.get("client_secret")
    if secret and method == "client_secret_basic":
        return httpx.BasicAuth(reg["client_id"], secret)
    data["client_id"] = reg["client_id"]
    if secret and method == "client_secret_post":
        data["client_secret"] = secret
    return None


# ---------- token store ----------

_TOKENS = {}
_TOKENS_LOCK = threading.Lock()
_REFRESH_LOCKS = {}


def _persist_key():
    raw = (os.environ.get("TS_CONGAROO_TOKEN_KEY") or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).digest() if raw else None


def _save_tokens(username, rec):
    with _TOKENS_LOCK:
        _TOKENS[username] = rec
    key = _persist_key()
    if not key:
        return
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(rec).encode("utf-8"), username.encode("utf-8"))
    entry = {"nonce": _b64url(nonce), "ciphertext": _b64url(ct), "saved_at": _iso()}
    storage.mutate_json(_tokens_path(), lambda d: d.__setitem__(username, entry), {})


def _load_tokens(username):
    with _TOKENS_LOCK:
        rec = _TOKENS.get(username)
    if rec:
        return rec
    key = _persist_key()
    if not key:
        return None
    entry = (storage.read_json(_tokens_path(), {}) or {}).get(username)
    if not entry:
        return None
    try:
        pad = lambda s: s + "=" * (-len(s) % 4)       # noqa: E731
        pt = AESGCM(key).decrypt(base64.urlsafe_b64decode(pad(entry["nonce"])),
                                 base64.urlsafe_b64decode(pad(entry["ciphertext"])),
                                 username.encode("utf-8"))
        rec = json.loads(pt)
    except (InvalidTag, ValueError, KeyError):
        # Wrong/rotated key or a record moved between users: unusable, not fatal.
        return None
    with _TOKENS_LOCK:
        _TOKENS[username] = rec
    return rec


def _forget_tokens(username):
    with _TOKENS_LOCK:
        _TOKENS.pop(username, None)
    _SESSIONS.pop(username, None)
    _TOOL_CACHE.pop(username, None)
    _META_CACHE.pop(username, None)
    if os.path.exists(_tokens_path()):
        storage.mutate_json(_tokens_path(), lambda d: d.pop(username, None), {})


def is_connected(username):
    return bool(_load_tokens(username))


def _account_from_id_token(id_token):
    """Display-only: who signed in. Not verified -- it decides nothing."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("email") or claims.get("name") or claims.get("sub")
    except Exception:                                 # noqa: BLE001
        return None


def _token_record(j, base):
    rec = dict(base)
    rec["access_token"] = j["access_token"]
    if j.get("refresh_token"):
        rec["refresh_token"] = j["refresh_token"]
    exp = j.get("expires_in")
    rec["expires_at"] = _now() + float(exp) if exp else None
    if j.get("scope"):
        rec["scope"] = j["scope"]
    if j.get("id_token"):
        rec["account"] = _account_from_id_token(j["id_token"]) or rec.get("account")
    return rec


# ---------- sign-in ----------

_PENDING = {}
_PREP_LOCK = {"lock": None}
_WARMED = set()


def _prep_lock():
    # Created lazily: an asyncio.Lock belongs to the loop that first uses it.
    if _PREP_LOCK["lock"] is None:
        _PREP_LOCK["lock"] = asyncio.Lock()
    return _PREP_LOCK["lock"]


async def _prepare(redirect):
    """Discovery + client registration, serialized. The first-ever Connect
    took 14.7 s here (cold TLS to two Workato hosts + registration) and a
    second click landed mid-way and registered a second client; the lock
    makes the second click wait for, and reuse, the first one's work."""
    async with _prep_lock():
        t0 = time.monotonic()
        async with _http() as client:
            disc = await discover(client)
            t1 = time.monotonic()
            reg = await _client_registration(client, disc, redirect)
        t2 = time.monotonic()
        if t2 - t0 > 2:
            print(f"[congaroo] sign-in prep took {t2 - t0:.1f}s "
                  f"(discovery {t1 - t0:.1f}s, registration {t2 - t1:.1f}s)", flush=True)
        return disc, reg


async def warm(request_base_url=None):
    """Do discovery + registration ahead of the first click, so Connect is
    instant. Called (fire-and-forget) when a not-yet-connected user's status
    is read. Never raises."""
    redirect = redirect_uri(request_base_url)
    if redirect in _WARMED or not available():
        return
    _WARMED.add(redirect)
    try:
        await _prepare(redirect)
    except Exception as e:                            # noqa: BLE001
        _WARMED.discard(redirect)
        print(f"[congaroo] warm-up failed: {e}", flush=True)


def _prune_pending():
    cutoff = _now() - PENDING_TTL
    for k in [k for k, v in _PENDING.items() if v["at"] < cutoff]:
        _PENDING.pop(k, None)


async def start_authorization(username, request_base_url=None):
    """Return the URL to send the user's browser to."""
    if not available():
        raise CongarooError("Congaroo is turned off on this server.", "congaroo_disabled")
    redirect = redirect_uri(request_base_url)
    disc, reg = await _prepare(redirect)
    verifier = _b64url(secrets.token_bytes(48))                 # 64 chars, within 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(32))
    _prune_pending()
    _PENDING[state] = {"username": username, "verifier": verifier, "redirect_uri": redirect,
                       "reg": reg, "at": _now()}
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": disc["resource"],
    }
    scope = _scope(disc)
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in disc["as"]["authorization_endpoint"] else "?"
    return disc["as"]["authorization_endpoint"] + sep + urllib.parse.urlencode(params)


async def complete_authorization(state, code, cookie_username=None):
    """Exchange the code. Returns the username the sign-in belonged to.

    The callback is a cross-site redirect, so the session cookie may or may
    not arrive with it; what binds the code to a user is the single-use,
    unguessable `state`. If a cookie does arrive and names someone else, the
    sign-in is refused rather than attached to either account."""
    _prune_pending()
    pending = _PENDING.pop(state or "", None)
    if not pending:
        raise CongarooError("This Congaroo sign-in link has expired or was already used. "
                            "Start again from Chat settings.", "congaroo_state")
    if cookie_username and cookie_username != pending["username"]:
        raise CongarooError("This Congaroo sign-in was started by a different user of this app.",
                            "congaroo_state")
    async with _http() as client:
        disc = await discover(client)
        reg = pending["reg"]
        data = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"],
                "resource": disc["resource"]}
        auth = _client_auth(reg, data)
        try:
            r = await client.post(disc["as"]["token_endpoint"], data=data, auth=auth,
                                  headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise CongarooError(f"Token exchange with Congaroo failed: {e.__class__.__name__}.",
                                "congaroo_token")
    if r.status_code != 200:
        raise CongarooError(f"Congaroo refused the sign-in (HTTP {r.status_code}): {r.text[:300]}",
                            "congaroo_token")
    j = r.json()
    if not j.get("access_token"):
        raise CongarooError("Congaroo's token response had no access_token.", "congaroo_token")
    base = {"client_id": reg["client_id"], "client_secret": reg.get("client_secret"),
            "auth_method": reg.get("auth_method") or "none",
            "token_endpoint": disc["as"]["token_endpoint"],
            "revocation_endpoint": disc["as"].get("revocation_endpoint"),
            "resource": disc["resource"], "connected_at": _iso()}
    _SESSIONS.pop(pending["username"], None)
    _TOOL_CACHE.pop(pending["username"], None)
    _save_tokens(pending["username"], _token_record(j, base))
    return pending["username"]


async def _refresh(username, client, rec):
    if not rec.get("refresh_token"):
        _forget_tokens(username)
        raise NotConnected("Your Congaroo sign-in has expired. Reconnect it from Chat settings.")
    data = {"grant_type": "refresh_token", "refresh_token": rec["refresh_token"],
            "resource": rec.get("resource")}
    auth = _client_auth(rec, data)
    try:
        r = await client.post(rec["token_endpoint"], data=data, auth=auth,
                              headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise CongarooError(f"Could not refresh the Congaroo sign-in: {e.__class__.__name__}.",
                            "congaroo_unreachable")
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not isinstance(j, dict) or not j.get("access_token"):
        _forget_tokens(username)
        raise NotConnected("Your Congaroo sign-in has expired. Reconnect it from Chat settings.")
    new = _token_record(j, {k: v for k, v in rec.items()
                                   if k not in ("access_token", "expires_at")})
    _save_tokens(username, new)
    return new


async def _access_token(username, client, force_refresh=False):
    rec = _load_tokens(username)
    if not rec:
        raise NotConnected()
    exp = rec.get("expires_at")
    if force_refresh or (exp and _now() > exp - REFRESH_SKEW):
        lock = _REFRESH_LOCKS.setdefault(username, asyncio.Lock())
        async with lock:
            latest = _load_tokens(username) or rec
            # Another request may have refreshed while this one waited.
            if latest.get("access_token") != rec.get("access_token") and not force_refresh:
                return latest["access_token"]
            rec = await _refresh(username, client, latest)
    return rec["access_token"]


async def disconnect(username):
    """Forget the user's tokens; revoke the refresh token if the server allows."""
    rec = _load_tokens(username)
    _forget_tokens(username)
    if rec and rec.get("revocation_endpoint") and rec.get("refresh_token"):
        data = {"token": rec["refresh_token"], "token_type_hint": "refresh_token"}
        auth = _client_auth(rec, data)
        try:
            async with _http() as client:
                await client.post(rec["revocation_endpoint"], data=data, auth=auth)
        except Exception:                             # noqa: BLE001 - best effort
            pass


def status(username):
    rec = _load_tokens(username)
    return {
        "available": available(),
        "url": server_url(),
        "connected": bool(rec),
        "account": (rec or {}).get("account"),
        "connected_at": (rec or {}).get("connected_at"),
        "persisted": bool(_persist_key()),
    }


# ---------- MCP over Streamable HTTP ----------

_SESSIONS = {}      # username -> {"id": str|None, "protocol": str}
_TOOL_CACHE = {}    # username -> {"at": t, "tools": [...]}
_META_CACHE = {}    # username -> {remote_name: {"at": t, "result": ...}}
_RPC_ID = {"n": 0}


def _next_id():
    _RPC_ID["n"] += 1
    return _RPC_ID["n"]


def _init_payload(rid):
    return {"jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "ts-debug-helper", "version": "1"}}}


def _headers(token, session):
    h = {"Authorization": f"Bearer {token}",
         "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json",
         "MCP-Protocol-Version": (session or {}).get("protocol") or PROTOCOL_VERSION}
    if session and session.get("id"):
        h["Mcp-Session-Id"] = session["id"]
    return h


def _parse_rpc(resp, want_id):
    ctype = (resp.headers.get("content-type") or "").lower()
    messages = []
    if "text/event-stream" in ctype:
        data = []
        for line in resp.text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line.strip() == "" and data:
                try:
                    messages.append(json.loads("\n".join(data)))
                except ValueError:
                    pass
                data = []
    else:
        try:
            j = resp.json()
        except ValueError:
            raise CongarooError(f"Congaroo sent an unreadable response ({ctype or 'no content type'}).",
                                "congaroo_protocol")
        messages = j if isinstance(j, list) else [j]
    for m in messages:
        if isinstance(m, dict) and m.get("id") == want_id:
            return m
    raise CongarooError("Congaroo's response did not answer the request.", "congaroo_protocol")


async def _initialize(client, username, token):
    rid = _next_id()
    r = await client.post(server_url(), headers=_headers(token, None), json=_init_payload(rid))
    if r.status_code == 401:
        raise _Unauthorized()
    if r.status_code >= 400:
        raise CongarooError(f"Congaroo refused to start a session (HTTP {r.status_code}): "
                            f"{r.text[:200]}", "congaroo_http")
    msg = _parse_rpc(r, rid)
    if "error" in msg:
        raise CongarooError(f"Congaroo initialize failed: {msg['error'].get('message')}",
                            "congaroo_protocol")
    session = {"id": r.headers.get("mcp-session-id"),
               "protocol": (msg.get("result") or {}).get("protocolVersion") or PROTOCOL_VERSION}
    note = await client.post(server_url(), headers=_headers(token, session),
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    if note.status_code == 401:
        raise _Unauthorized()
    _SESSIONS[username] = session
    return session


async def _rpc(username, method, params=None):
    async with _http() as client:
        force = False
        for _attempt in range(3):
            try:
                token = await _access_token(username, client, force_refresh=force)
                session = _SESSIONS.get(username) or await _initialize(client, username, token)
                rid = _next_id()
                payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
                r = await client.post(server_url(), headers=_headers(token, session), json=payload)
            except _Unauthorized:
                _SESSIONS.pop(username, None)
                force = True
                continue
            except httpx.TimeoutException:
                raise CongarooError("Congaroo did not answer in time.", "congaroo_timeout")
            except httpx.HTTPError as e:
                raise CongarooError(f"Could not reach Congaroo: {e.__class__.__name__}.",
                                    "congaroo_unreachable")
            if r.status_code == 401:
                _SESSIONS.pop(username, None)
                force = True
                continue
            if r.status_code == 404 and session.get("id"):
                _SESSIONS.pop(username, None)          # session expired server-side
                continue
            if r.status_code >= 400:
                raise CongarooError(f"Congaroo returned HTTP {r.status_code}: {r.text[:200]}",
                                    "congaroo_http")
            msg = _parse_rpc(r, rid)
            if "error" in msg:
                raise CongarooError(f"Congaroo error: {msg['error'].get('message')}", "congaroo_rpc")
            return msg.get("result") or {}
    _forget_tokens(username)
    raise NotConnected("Congaroo no longer accepts your sign-in. Reconnect it from Chat settings.")


async def list_tools(username, force=False):
    cached = _TOOL_CACHE.get(username)
    if cached and not force and _now() - cached["at"] < TOOLS_TTL:
        return cached["tools"]
    tools, cursor = [], None
    for _page in range(5):
        result = await _rpc(username, "tools/list", {"cursor": cursor} if cursor else {})
        tools.extend(result.get("tools") or [])
        cursor = result.get("nextCursor")
        if not cursor:
            break
    # Read-only by design: a tool that declares itself as writing is not offered.
    tools = [t for t in tools if (t.get("annotations") or {}).get("readOnlyHint") is not False]
    _TOOL_CACHE[username] = {"at": _now(), "tools": tools}
    return tools


def exposed_name(remote):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", remote or "").strip("_").lower()
    return (TOOL_PREFIX + slug)[:64]


def is_congaroo_tool(name):
    return bool(name) and name.startswith(TOOL_PREFIX)


DESCRIPTION_PREFIX = ("[Congaroo: Conga's internal product knowledge -- KB articles, documentation, "
                      "support cases and JIRAs. Use for how Conga's managed packages behave out of the "
                      "box, known defects and fixes; NOT for this org's customization.] ")


async def tool_specs(username):
    """(name, description, input schema) for each Congaroo tool, under its
    chat-safe exposed name."""
    out = []
    for t in await list_tools(username):
        out.append({"name": exposed_name(t.get("name")),
                    "description": DESCRIPTION_PREFIX + (t.get("description") or "").strip(),
                    "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}})
    return out


async def _remote_name(username, exposed):
    for t in await list_tools(username):
        if exposed_name(t.get("name")) == exposed:
            return t.get("name")
    return None


def _plain_result(result):
    if result.get("structuredContent") not in (None, {}):
        value = result["structuredContent"]
    else:
        texts = [c.get("text") for c in (result.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
        joined = "\n".join(texts)
        try:
            value = json.loads(joined) if joined else {}
        except ValueError:
            value = {"result": joined}
    if result.get("isError"):
        msg = value if isinstance(value, str) else json.dumps(value, default=str)[:600]
        return {"error": f"Congaroo tool error: {msg}"}
    if not isinstance(value, dict):
        value = {"result": value}
    return value


# ---------- result shaping ----------
#
# Congaroo answers are big for what they carry. A search hit is ~2.8 KB: the
# full text, an HTML-highlighted `caption` duplicating part of it, two
# scores, a metadata block, and a dozen "Field:- No Information Available"
# lines. Five hits for three queries is ~42 KB, and everything sits under one
# top-level key (`result`) -- so the chat's generic whole-key trimmer, built
# for component cards, dropped the ENTIRE answer and told the model to call
# get_component. Seen in the first live chat (2026-10-26): three of five
# Congaroo calls reached the model as {"omitted": true}. Shape here instead,
# keeping every hit's identity and url and cutting body text first.

_EMPTY_LINE_RE = re.compile(r"^[^\n]{0,80}:-[ \t]*(No Information Available|N/?A|None)?[ \t]*$",
                            re.IGNORECASE | re.MULTILINE)
_BLANKS_RE = re.compile(r"\n{3,}")
CONTENT_STEPS = (6000, 3500, 2200, 1400, 900, 550, 300)


def _size(obj):
    return len(json.dumps(obj, default=str))


def _clean_text(text):
    text = _EMPTY_LINE_RE.sub("", text or "")
    return _BLANKS_RE.sub("\n\n", text).strip()


def _unwrap(value):
    # {"result": {...}} is Workato's envelope, not information.
    while isinstance(value, dict) and len(value) == 1 and isinstance(value.get("result"), (dict, list)):
        value = value["result"]
    return value


def _compact_hit(hit, query):
    md = hit.get("metadata") or {}
    scores = hit.get("scores") or {}
    out = {
        "title": hit.get("title"),
        "url": hit.get("url"),
        "document_number": md.get("documentNumber") or hit.get("id"),
        "source": md.get("source"),
        "category": md.get("category") or None,
        "classification": md.get("classification"),
        "modified": (md.get("modifiedDate") or "")[:10] or None,
        "score": scores.get("rerankerScore"),
        "query": query,
        "content": _clean_text(hit.get("content") or hit.get("caption") or ""),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _shape_search(value, budget):
    """value has `responses: [{results: [...], context: {query}}]` or a bare
    `results: [...]` (the similar-document tool)."""
    groups = value.get("responses") if isinstance(value.get("responses"), list) else [value]
    hits, seen, dupes = [], {}, 0
    for g in groups:
        if not isinstance(g, dict):
            continue
        query = (g.get("context") or {}).get("query")
        for h in g.get("results") or []:
            if not isinstance(h, dict):
                continue
            key = h.get("id") or h.get("url") or h.get("title")
            if key in seen:
                dupes += 1
                also = seen[key].setdefault("also_matched", [])
                if query and query not in also and query != seen[key].get("query"):
                    also.append(query)
                continue
            c = _compact_hit(h, query)
            seen[key] = c
            hits.append(c)
    out = {"results": hits, "result_count": len(hits)}
    if dupes:
        out["duplicates_merged"] = dupes
    if _size(out) <= budget:
        return out
    cap = None
    for cap in CONTENT_STEPS:
        for h in hits:
            body = h.get("content", "")
            if len(body) > cap:
                h["content"] = body[:cap].rstrip() + " [...]"
        if _size(out) <= budget:
            break
    # Lowest-ranked go first; the model was told results are ranked. The trim
    # note is part of what must fit, so it is attached before measuring.
    out["_trimmed"] = {}
    dropped = []
    order = sorted(range(len(hits)), key=lambda i: (hits[i].get("score") or 0))

    def note():
        cut = sum(1 for h in hits if h is not None and h.get("content", "").endswith("[...]"))
        out["_trimmed"] = {
            "note": (f"Shaped to fit: {cut} result bodies cut to ~{cap} chars"
                     + (f", {len(dropped)} lowest-ranked results dropped" if dropped else "")
                     + ". Every result shown is complete in title/url/document_number."),
            "dropped": [{"title": d.get("title"), "url": d.get("url")} for d in dropped[:8]]
                       + ([{"more": len(dropped) - 8}] if len(dropped) > 8 else []),
            "how_to_get_more": ("To read one result in full, search again with a single precise "
                                "query and limit 1-2, or call the similar-document tool with its "
                                "document_number. Do not conclude a detail is absent from a cut body."),
        }
        if not dropped:
            out["_trimmed"].pop("dropped")
        out["results"] = [h for h in hits if h is not None]
        out["result_count"] = len(out["results"])

    note()
    for i in order:
        if _size(out) <= budget or out["result_count"] <= 1:
            break
        dropped.append(hits[i])
        hits[i] = None
        note()
    return out


def _compact_metadata(value):
    """The filter catalogue: ~27 KB raw, mostly `value: ""` and nulls."""
    out = {}
    for name, items in value.items():
        if not isinstance(items, list):
            out[name] = items
            continue
        rows = []
        for it in items:
            if not isinstance(it, dict):
                rows.append(it)
                continue
            row = {"key": it.get("key")}
            if it.get("text") and it.get("text") != it.get("key"):
                row["text"] = str(it["text"]).strip()
            for src, dst in (("availableInSources", "sources"), ("availableInSubSources", "sub_sources"),
                             ("availableInClassification", "classifications")):
                v = it.get(src)
                if v:
                    row[dst] = sorted(set(v))
            rows.append(row)
        out[name] = rows
    out["_how_to_use"] = ("Filter values are the `key`s. filters.sources[].name = a sources key "
                          "(scopes = subSources keys valid for it); filters.category = categories "
                          "keys; filters.classification = internal/public. Tags only apply to the "
                          "sources listed on each tag.")
    return out


def _shrink_generic(value, budget):
    cap = None
    for cap in (4000, 2000, 1000, 500, 250, 120):
        def walk(v):
            if isinstance(v, str):
                return v if len(v) <= cap else v[:cap] + " [...]"
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            return v
        shaped = walk(value)
        if _size(shaped) <= budget:
            if isinstance(shaped, dict):
                shaped["_trimmed"] = {"note": f"Long text fields cut to ~{cap} chars to fit."}
            return shaped
    return value                                      # let the chat's last-resort cut handle it


def shape_result(remote, value, budget):
    """Make a Congaroo result fit `budget` characters without losing which
    documents it found."""
    if not isinstance(value, dict) or value.get("error"):
        return value
    inner = _unwrap(value)
    if isinstance(inner, dict) and (isinstance(inner.get("responses"), list)
                                    or isinstance(inner.get("results"), list)):
        return _shape_search(inner, budget)
    if "metadata" in (remote or "").lower() and isinstance(inner, dict):
        compact = _compact_metadata(inner)
        return compact if _size(compact) <= budget else _shrink_generic(compact, budget)
    if not isinstance(inner, dict):
        inner = {"result": inner}
    return inner if _size(inner) <= budget else _shrink_generic(inner, budget)


async def call_tool(username, exposed, args, budget=None):
    """Run one Congaroo tool as `username`. Returns a plain dict; failures
    come back as {"error": ...} so one bad call never ends a chat turn --
    except NotConnected, which the caller turns into a visible notice."""
    remote = await _remote_name(username, exposed)
    if not remote:
        return {"error": f"No Congaroo tool named {exposed}."}
    args = args or {}
    is_meta = "metadata" in remote.lower() and not args
    if is_meta:
        hit = _META_CACHE.get(username, {}).get(remote)
        if hit and _now() - hit["at"] < METADATA_TTL:
            return hit["result"]
    try:
        value = _plain_result(await _rpc(username, "tools/call", {"name": remote, "arguments": args}))
    except NotConnected:
        raise
    except CongarooError as e:
        return {"error": str(e)}
    if budget:
        try:
            value = shape_result(remote, value, budget)
        except Exception:                             # noqa: BLE001 - never lose a result to shaping
            pass
    if is_meta and not value.get("error"):
        _META_CACHE.setdefault(username, {})[remote] = {"at": _now(), "result": value}
    return value
