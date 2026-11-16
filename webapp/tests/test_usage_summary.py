"""
Usage -> Executive summary (app/usage_summary.py, GET /api/usage/summary).

Seeds both ledgers and the registry with known records in the current and
previous windows, then checks: the headline figures, the period-over-period
comparison, inventory, the breakdowns, and scoping -- an admin sees everyone
or one account, anyone else is pinned to themselves and their visible orgs
whatever `username` they pass.

Also writes tests/fixtures/usage_summary.json from the real payload so the UI
test (tests/test_ui_activity.js) renders the server's actual shape.

Run:  python -m pytest tests/test_usage_summary.py -q
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-usage-summary-test-")
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

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import activity, auth, usage, usage_summary  # noqa: E402

FAILURES = []
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "usage_summary.json")


def check(label, condition, extra=""):
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))


TODAY = datetime.datetime.utcnow().date()


def ts(days_ago, hour=12):
    d = TODAY - datetime.timedelta(days=days_ago)
    return f"{d.isoformat()}T{hour:02d}:00:00Z"


def act(days_ago, username, action, channel="web", org_id=None, ok=True, meta=None, kind="action"):
    rec = {"kind": kind, "username": username, "role": "user", "channel": channel, "action": action,
           "category": "x", "org_id": org_id, "tool": None, "client": None,
           "status": 200 if ok else 500, "ok": ok, "ms": 10, "at": ts(days_ago)}
    if meta:
        rec["meta"] = meta
    day = TODAY - datetime.timedelta(days=days_ago)
    storage.append_jsonl(activity._day_path(day), rec)


def turn(days_ago, username, org_id, tokens, ok=True, cost=None):
    rec = {"at": ts(days_ago), "username": username, "org_id": org_id, "model": "gpt-test",
           "provider": "azure", "prompt_tokens": tokens - 100, "completion_tokens": 100,
           "total_tokens": tokens, "cost": cost or 0.0, "cost_reported": cost is not None,
           "tool_calls": 2, "tool_rounds": 1, "duration_ms": 4000, "ok": ok}
    day = TODAY - datetime.timedelta(days=days_ago)
    storage.append_jsonl(usage._day_path(day), rec)


def seed():
    auth.bootstrap_admin()
    storage.save_registry({
        "00DORG1": {"name": "Acme Prod", "account": "Acme", "owner": "dana", "visibility": "public",
                    "first_onboarded_at": ts(2), "component_counts": {"apex_classes": 100, "flows": 20}},
        "00DORG2": {"name": "Acme UAT", "account": "Acme", "owner": "admin", "visibility": "public",
                    "first_onboarded_at": ts(40), "component_counts": {"apex_classes": 50}},
        "00DORG3": {"name": "Globex", "account": "Globex", "owner": "sam", "visibility": "private",
                    "first_onboarded_at": ts(3), "component_counts": {"apex_classes": 10}},
    })
    auth.create_user("dana", "danapassword123", "user", created_by="admin")
    auth.create_user("sam", "sampassword1234", "user", created_by="admin")
    # current window (last 7 days): dana 3 questions, sam 1 (failed)
    turn(0, "dana", "00DORG1", 1000)
    turn(1, "dana", "00DORG1", 2000)
    turn(2, "dana", "00DORG2", 3000)
    turn(1, "sam", "00DORG3", 500, ok=False)
    act(0, "dana", "kb.field_writers", org_id="00DORG1")
    act(0, "dana", "kb.search", channel="mcp-remote", org_id="00DORG1")
    act(1, "dana", "log.parse", meta={"exceptions": 2, "size_kb": 40})
    act(1, "dana", "incident.file", org_id="00DORG1", meta={"has_log": True})
    act(2, "dana", "incident.resolve", org_id="00DORG1")
    act(2, "sam", "kb.component", org_id="00DORG3")
    act(3, "sam", "kb.component", org_id="00DORG3", ok=False)
    act(3, "sam", "auth.login")
    act(3, "sam", "ui.view", kind="ui")
    # previous window (8-14 days ago): dana 1 question, one lookup
    turn(9, "dana", "00DORG1", 4000)
    act(9, "dana", "kb.search", org_id="00DORG1")


def main():
    seed()
    print("\n-- module: admin, everyone, 7 days --")
    r = usage_summary.summary(7, registry=storage.load_registry(), users=auth.list_users())
    c, p, inv = r["current"], r["previous"], r["inventory"]
    check("window is 7 days ending today", r["days"] == 7 and r["to"] == TODAY.isoformat())
    check("previous window is the 7 days before", r["previous_to"] == (TODAY - datetime.timedelta(days=7)).isoformat())
    check("questions counted from the token ledger", c["questions"] == 4, c["questions"])
    check("failed questions counted", c["failed_questions"] == 1 and c["answer_rate"] == 75.0)
    check("tokens summed", c["tokens"] == 6500, c["tokens"])
    check("active people span both ledgers", c["active_users"] == 2, c["active_users"])
    check("org lookups count only kb.* that succeeded", c["kb_lookups"] == 3, c["kb_lookups"])
    check("MCP calls", c["mcp_calls"] == 1 and c["mcp_users"] == 1)
    check("logs parsed (standalone + via incident)", c["logs_parsed"] == 2, c["logs_parsed"])
    check("exceptions found", c["exceptions_found"] == 2)
    check("incidents and fixes", c["incidents_filed"] == 1 and c["resolutions"] == 1)
    check("orgs worked on across both ledgers", c["orgs_worked_on"] == 3, c["orgs_worked_on"])
    check("accounts worked on", c["accounts_worked_on"] == 2)
    check("orgs connected in window from the registry", c["orgs_connected"] == 2, c["orgs_connected"])
    check("failed actions", c["errors"] == 1)
    check("Azure cost is 'cannot say', not zero", c["cost_available"] is False)
    check("previous window questions", p["questions"] == 1 and p["tokens"] == 4000)
    check("previous has data", r["previous_has_data"] is True)
    check("inventory: orgs / accounts / components", inv["orgs_total"] == 3 and inv["accounts_total"] == 2
          and inv["components_indexed"] == 180, inv)
    check("inventory: user accounts", inv["users_total"] == 3, inv["users_total"])
    check("trend has one row per day", len(r["trend"]) == 7 and sum(d["questions"] for d in r["trend"]) == 4)
    check("top account is Acme", r["top_accounts"][0]["account"] == "Acme")
    check("org names come from the registry", r["top_orgs"][0]["org_name"] == "Acme Prod", r["top_orgs"][0])
    check("top people ranked", r["top_users"][0]["username"] == "dana")
    check("sign-ins are not a 'feature'", all(f["action"] != "auth.login" for f in r["top_features"]))
    check("channel mix present", r["channels"].get("mcp-remote") == 1)

    print("\n-- endpoint + scoping --")
    admin = TestClient(app)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"}).status_code == 200
    a = admin.get("/api/usage/summary?days=7").json()
    check("admin sees everyone", a["scope"] == "all users" and a["current"]["questions"] == 4)
    check("admin gets people + inventory users", a["top_users"] is not None and a["inventory"]["users_total"] == 3)
    a1 = admin.get("/api/usage/summary?days=7&username=sam").json()
    check("admin can narrow to one account", a1["scope"] == "sam" and a1["current"]["questions"] == 1)
    check("narrowed: orgs connected = that account's", a1["current"]["orgs_connected"] == 1)
    check("narrowed: no people table", a1["top_users"] is None)

    dana = TestClient(app)
    assert dana.post("/api/auth/login", json={"username": "dana", "password": "danapassword123"}).status_code == 200
    d = dana.get("/api/usage/summary?days=7&username=sam").json()
    check("a user is pinned to themselves whatever they pass", d["scope"] == "dana" and d["current"]["questions"] == 3)
    check("a user gets no people table and no account list", d["top_users"] is None and d["inventory"]["users_total"] is None)
    check("a user's inventory is only orgs they can see", d["inventory"]["orgs_total"] == 2, d["inventory"])
    check("nothing about sam leaks into dana's breakdowns",
          all(o["org_id"] != "00DORG3" for o in d["top_orgs"]))
    check("unauthenticated is refused", TestClient(app).get("/api/usage/summary").status_code == 401)

    print("\n-- the summary route is not itself recorded as activity --")
    before = len(activity.load_records(1))
    admin.get("/api/usage/summary?days=7")
    check("viewing the summary adds no ledger rows", len(activity.load_records(1)) == before)
    check("catalog entry exists", ("GET", "/api/usage/summary") in activity.CATALOG)

    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"admin": a, "admin_user": a1, "user": d}, f, indent=1)
    print(f"\n  fixture written: {FIXTURE}")


def test_usage_summary():
    main()
    assert not FAILURES, FAILURES


if __name__ == "__main__":
    main()
    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nAll usage summary checks passed.")
    sys.exit(1 if FAILURES else 0)
