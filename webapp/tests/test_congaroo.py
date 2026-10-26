"""
Congaroo integration (build 34): OAuth discovery -> dynamic client
registration -> PKCE sign-in -> MCP tools in the chat loop -> refresh ->
disconnect, all against a loopback double of the Workato MCP server whose
metadata shape matches the real one (16406.apim.mcp.workato.com, issuer
id.workato.com/auth/servers/<id>, PKCE S256, public client, SSE replies).

Run:  python tests/test_congaroo.py
"""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="ts-congaroo-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
os.environ["TS_SKIP_ENV_FILE"] = "1"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
for k in ("TS_CONGAROO_CLIENT_ID", "TS_CONGAROO_SCOPES", "TS_PUBLIC_URL", "TS_CONGAROO_REDIRECT_URI"):
    os.environ.pop(k, None)
os.environ["TS_CONGAROO_TOKEN_KEY"] = "test-key-" + secrets.token_hex(16)

from app import storage  # noqa: E402
storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")
from app import chat_store  # noqa: E402
chat_store.CHATS_ROOT = os.path.join(_TMP, "chats")

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(name)


# ---------------- the Workato double ----------------

S = {"clients": {}, "codes": {}, "access": {}, "refresh": {}, "sessions": set(),
     "calls": [], "registrations": 0, "expire_next_access": False, "revoked": []}
ISSUER_PATH = "/auth/servers/xxTEST"

TOOLS = [
    {"name": "Fetch Metadata For Searching Conga's Knowledgebase",
     "description": "Always use this tool before searching Conga's knowledgebase.",
     "inputSchema": {"type": "object"}},
    {"name": "Search Conga's Knowledgebase",
     "description": "Use this tool to search Conga's knowledgebase.",
     "inputSchema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                     "properties": {"limit": {"type": "integer", "format": "int32"},
                                    "queries": {"type": "array", "items": {"type": "string"}}},
                     "required": ["limit", "queries"]}},
    {"name": "Delete Everything", "description": "would write", "inputSchema": {"type": "object"},
     "annotations": {"readOnlyHint": False}},
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, obj, headers=None):
        body = ("event: message\ndata: " + json.dumps(obj) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def do_GET(self):
        base = f"http://127.0.0.1:{PORT}"
        p = urllib.parse.urlsplit(self.path)
        if p.path == "/.well-known/oauth-protected-resource":
            return self._json(200, {"resource": base, "authorization_servers": [base + ISSUER_PATH],
                                    "bearer_methods_supported": ["header"]})
        if p.path == "/.well-known/oauth-authorization-server" + ISSUER_PATH:
            iss = base + ISSUER_PATH
            return self._json(200, {"issuer": iss, "authorization_endpoint": iss + "/authorize",
                                    "token_endpoint": iss + "/token", "registration_endpoint": iss + "/clients",
                                    "revocation_endpoint": iss + "/revoke",
                                    "scopes_supported": ["openid", "profile", "email", "claudeai"],
                                    "code_challenge_methods_supported": ["S256"],
                                    "token_endpoint_auth_methods_supported": ["none"]})
        if p.path == ISSUER_PATH + "/authorize":
            q = dict(urllib.parse.parse_qsl(p.query))
            ok = (q.get("client_id") in S["clients"] and q.get("code_challenge_method") == "S256"
                  and q.get("redirect_uri") in S["clients"][q["client_id"]]["redirect_uris"]
                  and q.get("resource") == base)
            if not ok:
                return self._json(400, {"error": "invalid_request", "q": q})
            code = secrets.token_urlsafe(8)
            S["codes"][code] = q
            loc = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
            return
        self._json(404, {"error": "nope"})

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path)
        raw = self._body()
        if p.path == ISSUER_PATH + "/clients":
            req = json.loads(raw)
            S["registrations"] += 1
            cid = "cid_" + secrets.token_hex(4)
            S["clients"][cid] = req
            return self._json(201, {"client_id": cid, "token_endpoint_auth_method": "none", **req})
        if p.path == ISSUER_PATH + "/token":
            f = dict(urllib.parse.parse_qsl(raw))
            if f.get("grant_type") == "authorization_code":
                q = S["codes"].pop(f.get("code"), None)
                if not q:
                    return self._json(400, {"error": "invalid_grant"})
                chal = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if chal != q["code_challenge"] or f.get("redirect_uri") != q["redirect_uri"]:
                    return self._json(400, {"error": "invalid_grant", "why": "pkce"})
            elif f.get("grant_type") == "refresh_token":
                if f.get("refresh_token") not in S["refresh"]:
                    return self._json(400, {"error": "invalid_grant"})
                S["refresh"].pop(f["refresh_token"])
            else:
                return self._json(400, {"error": "unsupported_grant_type"})
            at, rt = "at_" + secrets.token_hex(6), "rt_" + secrets.token_hex(6)
            S["access"][at] = True
            S["refresh"][rt] = True
            claims = base64.urlsafe_b64encode(json.dumps({"email": "manshah@conga.com"}).encode()).rstrip(b"=").decode()
            return self._json(200, {"access_token": at, "refresh_token": rt, "expires_in": 3600,
                                    "token_type": "Bearer", "id_token": f"e30.{claims}.sig"})
        if p.path == ISSUER_PATH + "/revoke":
            S["revoked"].append(dict(urllib.parse.parse_qsl(raw)).get("token"))
            return self._json(200, {})
        if p.path == "/":
            tok = (self.headers.get("Authorization") or "")[7:]
            if S["expire_next_access"] and tok in S["access"]:
                S["access"].pop(tok)
                S["expire_next_access"] = False
            if tok not in S["access"]:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer resource_metadata="x"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            msg = json.loads(raw)
            if msg.get("method") == "initialize":
                sid = "sess_" + secrets.token_hex(4)
                S["sessions"].add(sid)
                return self._sse({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                             "serverInfo": {"name": "workato-double"}}},
                                 {"Mcp-Session-Id": sid})
            if self.headers.get("Mcp-Session-Id") not in S["sessions"]:
                return self._json(404, {"error": "unknown session"})
            if msg.get("method") == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if msg.get("method") == "tools/list":
                return self._sse({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": TOOLS}})
            if msg.get("method") == "tools/call":
                S["calls"].append(msg["params"])
                name = msg["params"]["name"]
                if name.startswith("Fetch Metadata"):
                    text = json.dumps({"sources": [{"name": "KB", "scopes": ["CPQ"]}]})
                elif S.get("big"):
                    text = json.dumps(big_search(3, 5))
                else:
                    text = json.dumps({"results": [{"title": "Pricing callback runs twice on reprice",
                                                    "url": "https://kb.conga.example/KB-123"}]})
                return self._sse({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        self._json(404, {"error": "nope"})


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = SERVER.server_address[1]
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
os.environ["TS_CONGAROO_URL"] = f"http://127.0.0.1:{PORT}/"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import auth, chat, congaroo, llm, secrets_store  # noqa: E402
import httpx  # noqa: E402


# ---------- synthetic Congaroo payloads, shaped like the real ones ----------
# Field names, envelope and sizes mirror what the live server returned on
# 2026-10-26 (metadata ~27 KB; a 3-query x 5-hit search ~42 KB). Text is
# invented: no real case or customer content belongs in the repo.

def _item(key, text, **avail):
    return {"key": key, "text": text, "value": "", "availableInClassification": avail.get("cls"),
            "availableInSources": avail.get("src"), "availableInSubSources": avail.get("sub")}


def big_metadata():
    srcs = ["case", "knowledgebase", "rfp-qa", "documentation", "api-documentation", "confluence",
            "jira", "sharepoint", "learning-center", "discussion-post"]
    return {"result": {
        "classifications": [_item("public", "Public"), _item("internal", "Internal")],
        "sources": [_item(k, k.title(), cls=["internal", "public"]) for k in srcs],
        "subSources": [_item(f"SUB{i}", f"Sub source {i}", src=["confluence"]) for i in range(25)],
        "categories": [_item(f"cat-{i}", f"Category number {i}", src=srcs[:8]) for i in range(36)],
        "subCategories": [_item(f"subcat-{i}", f"Sub category {i}", src=["documentation"]) for i in range(6)],
        "tags": [_item(f"Tag {i}", f"Tag {i}", src=["rfp-qa"]) for i in range(70)],
    }}


def _hit(q, i, uniq=True):
    body = ("Article Number:- 0000%d\nArticle Type:- Troubleshooting\nTitle:- Hit %d for %s\n"
            "Symptom:- No Information Available\nCause:- No Information Available\n"
            "Error Message:- No Information Available\nResolution:- " % (i, i, q)) + ("Step detail. " * 170)
    hid = f"knowledgebase_{q[:6]}_{i}" if uniq else "knowledgebase_shared"
    return {"id": hid, "title": f"Hit {i} for {q}", "content": body,
            "caption": "<b>highlighted</b> " + body[:300], "url": f"https://kb.example/{hid}",
            "scores": {"searchScore": 0.01, "rerankerScore": 3.0 - i * 0.1},
            "metadata": {"documentNumber": hid, "source": "knowledgebase", "subSource": "",
                         "category": ["cpq"], "subCategory": [], "classification": "internal",
                         "citationTitle": "KB", "createdDate": "2025-01-01T00:00:00.000+00:00",
                         "modifiedDate": "2026-01-02T00:00:00.000+00:00", "tags": []}}


def big_search(nq, nh):
    return {"result": {"responses": [
        {"results": [_hit(f"query{q}", i, uniq=not (i == 0 and q > 0)) for i in range(nh)],
         "context": {"query": f"query{q}", "filters": "search.in(classification, 'internal,public')"}}
        for q in range(nq)]}}


def unit_shaping():
    print("result shaping (unit)")
    budget = 24000 - 400
    raw = big_metadata()
    check("synthetic metadata is as large as the real one", len(json.dumps(raw)) > 20000, len(json.dumps(raw)))
    m = congaroo.shape_result("Fetch Metadata For Searching Conga's Knowledgebase", raw, budget)
    check("metadata fits the budget", len(json.dumps(m)) <= budget, len(json.dumps(m)))
    check("...keeps every source and category key",
          [r["key"] for r in m["sources"]][:3] == ["case", "knowledgebase", "rfp-qa"]
          and len(m["categories"]) == 36 and "sources" in m["categories"][0], m.get("sources", [])[:2])
    check("...drops the empty `value` noise and says how to use it",
          '"value"' not in json.dumps(m) and "_how_to_use" in m)

    raw = big_search(3, 5)
    check("synthetic search is ~42 KB like the live one", len(json.dumps(raw)) > 38000, len(json.dumps(raw)))
    r = congaroo.shape_result("Search Conga's Knowledgebase", raw, budget)
    text = json.dumps(r)
    check("search fits the budget", len(text) <= budget, len(text))
    check("...nothing is replaced by an 'omitted' marker", '"omitted"' not in text)
    check("...duplicates across queries are merged", r.get("duplicates_merged") == 1, r.get("duplicates_merged"))
    check("...every remaining hit keeps title, url and document number",
          all(h.get("title") and h.get("url") and h.get("document_number") for h in r["results"]))
    check("...all 14 unique hits survive (bodies cut, not hits)", r["result_count"] == 14, r["result_count"])
    check("...captions, scores noise and empty fields are gone",
          "caption" not in text and "No Information Available" not in text and "searchScore" not in text)
    check("...the trim note gives Congaroo advice, not get_component",
          "_trimmed" in r and "get_component" not in text and "similar-document" in r["_trimmed"]["how_to_get_more"])

    tight = congaroo.shape_result("Search Conga's Knowledgebase", big_search(5, 10), 9000)
    check("under a tight budget the lowest-ranked hits are dropped and listed",
          len(json.dumps(tight)) <= 9000 and tight["_trimmed"]["dropped"]
          and all(d.get("url") or "more" in d for d in tight["_trimmed"]["dropped"]), len(json.dumps(tight)))

    small = congaroo.shape_result("Search Conga's Knowledgebase", big_search(1, 1), budget)
    check("a small result is cleaned but not marked trimmed", "_trimmed" not in small and small["result_count"] == 1)

    content, truncated = chat._truncate(r, tool="congaroo_search_conga_s_knowledgebase")
    check("chat passes the shaped result through intact and flags it",
          truncated and json.loads(content)["result_count"] == 14)


def login(username, password):
    c = TestClient(app, base_url="http://testserver")
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def follow_authorize(url):
    """Play the browser: hit authorize, take the 302 back to our callback."""
    r = httpx.get(url, follow_redirects=False, trust_env=False)
    return r.status_code, r.headers.get("location")


def main():
    unit_shaping()
    with TestClient(app):
        auth.create_user("wendy", "password123", "user", created_by="admin:admin")
        auth.create_user("rory", "password123", "user", created_by="admin:admin")
        wendy = login("wendy", "password123")
        rory = login("rory", "password123")

        print("status before connecting")
        st = wendy.get("/api/congaroo/status").json()
        check("available, not enabled, not connected",
              st["available"] and not st["enabled"] and not st["connected"], st)

        print("toggle via prefs")
        r = wendy.post("/api/me/guide", json={"prefs": {"congaroo_enabled": True}})
        check("pref accepted", r.status_code == 200, r.text)
        check("status reports enabled", wendy.get("/api/congaroo/status").json()["enabled"])

        print("connect: discovery + dynamic registration + PKCE")
        r = wendy.post("/api/congaroo/connect")
        check("connect returns an authorize url", r.status_code == 200, r.text)
        body = r.json()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["authorize_url"]).query))
        check("redirect uri is this app's callback",
              body["redirect_uri"] == "http://testserver/api/congaroo/oauth/callback", body)
        check("PKCE S256 + resource + all advertised scopes",
              q.get("code_challenge_method") == "S256" and q.get("resource") == f"http://127.0.0.1:{PORT}"
              and q.get("scope") == "openid profile email claudeai", q)
        check("registered exactly once", S["registrations"] == 1)
        wendy.post("/api/congaroo/connect")
        check("registration is cached per redirect uri", S["registrations"] == 1, S["registrations"])

        print("concurrent first connects register only once")
        congaroo._DISCOVERY.update(at=0, value=None)
        before = S["registrations"]
        alt = "http://other-host:8000/"

        async def two():
            return await asyncio.gather(congaroo.start_authorization("wendy", alt),
                                        congaroo.start_authorization("wendy", alt))
        congaroo._PREP_LOCK["lock"] = None
        urls = asyncio.run(two())
        check("two simultaneous connects -> one registration", S["registrations"] == before + 1,
              S["registrations"] - before)
        check("...both got a usable sign-in url", all("code_challenge=" in u for u in urls))
        congaroo._PREP_LOCK["lock"] = None

        code, loc = follow_authorize(body["authorize_url"])
        check("authorize redirects back with a code", code == 302 and "code=" in (loc or ""), (code, loc))
        cb = urllib.parse.urlsplit(loc)
        # Rory's cookie on Wendy's state must be refused.
        r = rory.get(cb.path + "?" + cb.query)
        check("callback refuses another user's session", r.status_code == 400 and "different user" in r.text)
        # That state is now burned; start again for Wendy.
        code, loc = follow_authorize(wendy.post("/api/congaroo/connect").json()["authorize_url"])
        cb = urllib.parse.urlsplit(loc)
        anon = TestClient(app, base_url="http://testserver")
        r = anon.get(cb.path + "?" + cb.query)                    # no cookie: state alone binds it
        check("callback succeeds without a cookie", r.status_code == 200 and "Congaroo connected" in r.text,
              r.text[:300])
        r = anon.get(cb.path + "?" + cb.query)
        check("state is single-use", r.status_code == 400)
        st = wendy.get("/api/congaroo/status").json()
        check("wendy connected, account from id_token",
              st["connected"] and st["account"] == "manshah@conga.com", st)
        check("rory is not connected", not rory.get("/api/congaroo/status").json()["connected"])

        print("token storage")
        raw = open(congaroo._tokens_path(), encoding="utf-8").read()
        check("tokens on disk are encrypted", "at_" not in raw and "rt_" not in raw and "wendy" in raw)
        congaroo._TOKENS.clear()
        check("encrypted record reloads after a 'restart'", congaroo.is_connected("wendy"))

        print("tools: listing, naming, read-only filter")
        specs = asyncio.run(chat.congaroo_schemas("wendy"))
        names = [s_["function"]["name"] for s_ in specs]
        check("two read-only tools exposed with safe names",
              names == ["congaroo_fetch_metadata_for_searching_conga_s_knowledgebase",
                        "congaroo_search_conga_s_knowledgebase"], names)
        search = specs[1]["function"]["parameters"]
        check("$schema stripped from parameters", "$schema" not in search, search)

        print("chat turn uses Congaroo")
        STATE = {"n": 0, "tools_seen": None, "system": ""}

        async def fake_stream(creds, model, messages, tools=None, **kw):
            STATE["n"] += 1
            STATE["tools_seen"] = [t["function"]["name"] for t in (tools or [])]
            STATE["system"] = messages[0]["content"]
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            if STATE["n"] == 1:
                yield llm.Delta(tool_calls=[{"index": 0, "id": "c1", "function": {
                    "name": "congaroo_fetch_metadata_for_searching_conga_s_knowledgebase", "arguments": "{}"}}])
                return
            if STATE["n"] == 2:
                yield llm.Delta(tool_calls=[{"index": 0, "id": "c2", "function": {
                    "name": "congaroo_search_conga_s_knowledgebase",
                    "arguments": json.dumps({"limit": 3, "queries": ["pricing callback runs twice"]})}}])
                return
            yield llm.Delta(content="Known product behaviour: see KB-123. " +
                            ("saw-kb" if any("KB-123" in (m.get("content") or "") for m in tool_msgs) else ""))

        orig_stream, orig_creds = llm.stream_chat, secrets_store.require_creds
        llm.stream_chat = fake_stream
        secrets_store.require_creds = lambda ident: {"provider": "openrouter", "api_key": "x", "source": "shared"}
        try:
            async def run(username, chat_id):
                frames = []
                async for f in chat.run_turn({"username": username, "role": "user"}, chat_id,
                                             "Why does pricing run twice?", None, None, "m"):
                    frames.append(f)
                return "".join(frames)

            cid = chat_store.create_chat("wendy", org_id=None, title="t", model="m")["chat_id"]
            out = asyncio.run(run("wendy", cid))
            check("Congaroo tools offered without an org selected",
                  any(n.startswith("congaroo_") for n in STATE["tools_seen"] or []), STATE["tools_seen"])
            check("system prompt carries the Congaroo guidance", "CONGA PRODUCT KNOWLEDGE" in STATE["system"])
            check("both Congaroo calls reached the server with real names",
                  [c["name"] for c in S["calls"]] == ["Fetch Metadata For Searching Conga's Knowledgebase",
                                                       "Search Conga's Knowledgebase"], S["calls"])
            check("model saw the KB result and answered", "saw-kb" in out, out[-400:])
            check("no error frames", "event: error" not in out, out[:600])

            print("metadata cache")
            n_before = len(S["calls"])
            asyncio.run(congaroo.call_tool("wendy", "congaroo_fetch_metadata_for_searching_conga_s_knowledgebase", {}))
            check("repeat metadata call served from cache", len(S["calls"]) == n_before)

            print("a 42 KB search reaches the model as evidence, not 'omitted'")
            S["big"] = True
            STATE["n"] = 1          # next completion: the search call, then answer
            cid3 = chat_store.create_chat("wendy", org_id=None, title="t", model="m")["chat_id"]
            asyncio.run(run("wendy", cid3))
            S["big"] = False
            msgs = chat_store.load_messages("wendy", cid3)
            tm = [m for m in msgs if m.get("role") == "tool" and m.get("name", "").startswith("congaroo_search")]
            body = tm[-1]["content"] if tm else ""
            check("tool message holds the hits", '"result_count": 14' in body and '"omitted"' not in body, body[:300])
            disp = [c for m in msgs if m.get("role") == "assistant" for c in (m.get("tool_calls") or [])
                    if c["name"].startswith("congaroo_search")]
            check("...and the row is flagged as trimmed", bool(disp) and disp[-1].get("truncated") is True)

            print("expired access token -> refresh + retry")
            S["expire_next_access"] = True
            res = asyncio.run(congaroo.call_tool("wendy", "congaroo_search_conga_s_knowledgebase",
                                                 {"limit": 1, "queries": ["x"]}))
            check("call succeeded after a 401 by refreshing", "results" in res, res)

            print("toggle off / not connected")
            STATE["n"] = 99
            rory.post("/api/me/guide", json={"prefs": {"congaroo_enabled": True}})
            cid2 = chat_store.create_chat("rory", org_id=None, title="t", model="m")["chat_id"]
            out = asyncio.run(run("rory", cid2))
            check("enabled-but-unconnected user gets a notice",
                  "congaroo_not_connected" in out, out[:400])
            check("and no Congaroo tools", not any(n.startswith("congaroo_") for n in STATE["tools_seen"]))
            wendy.post("/api/me/guide", json={"prefs": {"congaroo_enabled": None}})
            STATE["n"] = 99
            out = asyncio.run(run("wendy", cid))
            check("toggle off removes the tools",
                  not any(n.startswith("congaroo_") for n in STATE["tools_seen"]), STATE["tools_seen"])
            check("toggle off removes the prompt section", "CONGA PRODUCT KNOWLEDGE" not in STATE["system"])
        finally:
            llm.stream_chat, secrets_store.require_creds = orig_stream, orig_creds

        print("server-wide switch")
        os.environ["TS_CONGAROO_ENABLED"] = "0"
        check("connect refused when disabled", wendy.post("/api/congaroo/connect").status_code == 403)
        check("status says unavailable", not wendy.get("/api/congaroo/status").json()["available"])
        os.environ.pop("TS_CONGAROO_ENABLED")

        print("disconnect")
        st = wendy.post("/api/congaroo/disconnect").json()
        check("disconnected", not st["connected"])
        check("refresh token revoked", len(S["revoked"]) == 1 and S["revoked"][0].startswith("rt_"), S["revoked"])
        check("removed from disk", "wendy" not in (storage.read_json(congaroo._tokens_path(), {}) or {}))

    print()
    print("FAILURES:" if FAILURES else "ALL PASS", *FAILURES, sep="\n  ")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
