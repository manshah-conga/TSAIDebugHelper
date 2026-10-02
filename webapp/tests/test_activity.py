"""
Activity analytics (app/activity.py): every channel is attributed, noise is
not recorded, content is never recorded, users see only themselves.

Channels exercised end to end against the real app:
  web         TestClient with a session cookie
  chat        chat.call_tool (the in-app agent's path)
  mcp-stdio   the FastMCP tool manager called with no channel set
  mcp-remote  JSON-RPC initialize + tools/call over POST /mcp
The MCP tool bodies loop back into the app through httpx; here that loopback
is pointed at the ASGI app in-process instead of a TCP port.

Run:  python -m pytest tests/test_activity.py -q
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-activity-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import activity, chat, auth  # noqa: E402
import mcp_server  # noqa: E402

LOG = """59.0 APEX_CODE,FINEST;APEX_PROFILING,INFO
12:00:00.0 (1)|EXECUTION_STARTED
12:00:00.0 (2)|CODE_UNIT_STARTED|[EXTERNAL]|01q000000000001|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (3)|EXCEPTION_THROWN|[12]|System.NullPointerException: Attempt to de-reference a null object
12:00:00.0 (4)|FATAL_ERROR|System.NullPointerException: Attempt to de-reference a null object
12:00:00.0 (5)|CODE_UNIT_FINISHED|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (6)|EXECUTION_FINISHED
"""
SECRET_QUERY = "CustomerSecretField__c"

FAILURES = []


def check(label, condition, extra=""):
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))


def _loopback_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://loopback",
                             timeout=30.0,
                             headers={**mcp_server._auth_headers(), **mcp_server._telemetry_headers()})


def events():
    return activity.load_records(1)


def seed_org(org_id, owner):
    reg = storage.load_registry()
    reg[org_id] = {"name": org_id, "org_name": org_id, "instance_url": f"https://{org_id}.my.salesforce.com",
                   "owner": owner, "visibility": "public", "account": "Acme Corp"}
    storage.save_registry(reg)


def main():
    mcp_server._client = _loopback_client
    with TestClient(app) as admin:
        r = admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        assert r.status_code == 200, r.text
        for n in ("alice", "bob"):
            r = admin.post("/api/admin/users", json={"username": n, "password": "password123", "role": "user"})
            assert r.status_code in (200, 201), r.text
        seed_org("acme", "alice")

        alice = TestClient(app)
        alice.post("/api/auth/login", json={"username": "alice", "password": "password123"})
        bad = TestClient(app).post("/api/auth/login", json={"username": "alice", "password": "wrong"})
        assert bad.status_code == 401

        print("\n-- web channel --")
        before = len(events())
        for _ in range(5):
            alice.get("/api/orgs")                      # page load: nonweb -> skipped
            alice.get("/api/auth/me")                   # never
        check("list/poll page loads are not recorded on web", len(events()) == before,
              f"{len(events()) - before} new")

        r = alice.get(f"/api/orgs/acme/search", params={"query": SECRET_QUERY})
        r = alice.post("/api/logs/normalize", data={"store": "true", "label": "npe"},
                       files={"log_file": ("debug.log", LOG.encode(), "text/plain")})
        check("log parse still works", r.status_code == 200, r.text[:200])
        ev = events()
        parse = [e for e in ev if e.get("action") == "log.parse"]
        check("log parse recorded once", len(parse) == 1, str(parse))
        if parse:
            p = parse[0]
            check("...on the web channel", p["channel"] == "web", p["channel"])
            check("...with size / lines / timing / exception facts",
                  all(k in p.get("meta", {}) for k in ("size_kb", "lines", "parse_ms", "exceptions")),
                  str(p.get("meta")))
            check("...and stored flag", p["meta"].get("stored") is True)
        check("search recorded as kb.search", any(e.get("action") == "kb.search" for e in ev))
        check("login recorded, failed guess not",
              sum(1 for e in ev if e.get("action") == "auth.login" and e.get("username") == "alice") == 1)
        raw = open(activity._day_path(activity._today()), encoding="utf-8").read()
        check("search text never reaches the ledger", SECRET_QUERY not in raw)
        check("log content never reaches the ledger", "NullPointerException" not in raw)

        print("\n-- in-app chat channel --")
        cookie = alice.cookies.get(auth.SESSION_COOKIE)
        res = asyncio.run(chat.call_tool("list_orgs", {}, cookie))
        check("chat tool call works through loopback", isinstance(res, dict) and "error" not in res, str(res)[:200])
        e = [x for x in events() if x.get("channel") == "chat"]
        check("chat tool call recorded with channel=chat", len(e) == 1, str(e))
        if e:
            check("...and tool name", e[0].get("tool") == "list_orgs", str(e[0]))
            check("...and attributed to alice", e[0].get("username") == "alice")

        print("\n-- stdio MCP channel --")
        _tid, api_tok = auth.create_token("bob", "user", kind="api", label="desktop")
        mcp_server.API_TOKEN = api_tok
        res = asyncio.run(mcp_server.mcp._tool_manager.call_tool(
            "normalize_log", {"log_text": LOG}, context=None, convert_result=False))
        check("stdio tool call works", isinstance(res, dict) and "normalized_log" in res, str(res)[:200])
        e = [x for x in events() if x.get("channel") == "mcp-stdio"]
        check("stdio call recorded as mcp-stdio", len(e) == 1, str(e))
        if e:
            check("...as a log parse by bob via normalize_log",
                  e[0]["action"] == "log.parse" and e[0]["username"] == "bob" and e[0]["tool"] == "normalize_log",
                  str(e[0]))
        mcp_server.API_TOKEN = ""

        print("\n-- remote MCP channel --")
        _tid2, remote_tok = auth.create_token("bob", "user", kind="api", label="claude")
        mc = TestClient(app, base_url="http://localhost:8000")
        hdr = {"Authorization": f"Bearer {remote_tok}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", "User-Agent": "test-agent/1"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}
        r = mc.post("/mcp", headers=hdr, content=json.dumps(init))
        check("initialize ok", r.status_code == 200, r.text[:200])
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_normalized_logs", "arguments": {}}}
        r = mc.post("/mcp", headers=hdr, content=json.dumps(call))
        check("tools/call ok", r.status_code == 200, r.text[:200])
        ev = events()
        conn = [x for x in ev if x.get("action") == "mcp.connect"]
        check("initialize recorded as mcp.connect with client name",
              len(conn) == 1 and conn[0].get("client") == "claude-ai", str(conn))
        rem = [x for x in ev if x.get("channel") == "mcp-remote" and x.get("action") != "mcp.connect"]
        check("remote tool call recorded as mcp-remote", len(rem) == 1, str(rem))
        if rem:
            check("...labelled with tool + client from the handshake",
                  rem[0].get("tool") == "list_normalized_logs" and (rem[0].get("client") or "").startswith("claude-ai"),
                  str(rem[0]))

        print("\n-- UI beacon --")
        r = alice.post("/api/activity/events", json={"events": [
            {"action": "ui.view", "meta": {"view": "logs"}},
            {"action": "ui.palette_run", "meta": {"command": "open-logs"}},
            {"action": "ui.evil", "meta": {}},
            {"action": "ui.view", "meta": {"view": "<script>alert(1)</script>"}},
        ]})
        check("beacon accepts allowlisted only", r.json().get("accepted") == 3, r.text)
        raw = open(activity._day_path(activity._today()), encoding="utf-8").read()
        check("unsafe meta dropped", "<script>" not in raw)
        check("beacon requires auth", TestClient(app).post("/api/activity/events", json={"events": []}).status_code == 401)

        print("\n-- reports --")
        rep = admin.get("/api/admin/activity?days=7").json()
        chans = {c["channel"]: c for c in rep["by_channel"]}
        check("all four channels present", all(k in chans for k in ("web", "chat", "mcp-stdio", "mcp-remote")),
              str(list(chans)))
        segs = {u["username"]: u["segment"] for u in rep["by_user"]}
        check("bob (MCP only, no LLM) segmented as such", segs.get("bob") == "MCP only (no in-app LLM)", str(segs))
        check("alice uses the in-app chat", segs.get("alice") == "In-app AI chat", str(segs))
        check("non-LLM user counted", rep["totals"]["non_llm_users"] >= 1, str(rep["totals"]))
        check("log parser totals across channels", rep["log_parser"]["parses"] == 2
              and rep["log_parser"]["by_channel"].get("mcp-stdio") == 1, str(rep["log_parser"]))
        tools = {t["tool"] for t in rep["tools"]}
        check("tool table lists MCP + chat tools", {"list_orgs", "normalize_log", "list_normalized_logs"} <= tools,
              str(tools))
        check("MCP clients table has claude-ai", any(c["client"].startswith("claude-ai") for c in rep["mcp_clients"]),
              str(rep["mcp_clients"]))
        check("org rollup carries account", any(o["org_id"] == "acme" and o["account"] == "Acme Corp"
                                                for o in rep["by_org"]), str(rep["by_org"]))
        check("ladder counts log parsers", next(s for s in rep["ladder"] if s["step"] == "log")["users"] == 2,
              str(rep["ladder"]))

        mine = alice.get("/api/activity/me?days=7").json()
        check("my report is mine only", all(e["username"] == "alice" for e in mine["recent"]))
        check("my report has no cross-user sections", "by_user" not in mine and "segments" not in mine)
        check("my report carries my summary", (mine.get("me") or {}).get("logs_parsed") == 1, str(mine.get("me")))
        check("admin report is admin-only", alice.get("/api/admin/activity").status_code == 403)

        csv_r = admin.get("/api/admin/activity/export?days=7")
        check("CSV export", csv_r.status_code == 200 and csv_r.text.startswith("at,kind,username"),
              csv_r.text[:80])

        print("\n-- account deletion --")
        admin.delete("/api/admin/users/bob")
        check("deleted user's activity is forgotten",
              not any(e.get("username") == "bob" for e in events()))

    if FAILURES:
        print("\nFAILURES:\n  " + "\n  ".join(FAILURES))
    return not FAILURES


def test_activity():
    assert main()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
