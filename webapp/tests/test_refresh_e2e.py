"""
End-to-end proof that one-click refresh really re-fetches and really reports
what moved: runs the mock Salesforce server in-process, connects an org
through the real API, refreshes it unchanged (expects "nothing changed"),
then mutates a class in the mock org and refreshes again (expects exactly
that one class reported as changed).

Run:  python tests/test_refresh_e2e.py
"""
import os
import sys
import time
import socket
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-refresh-e2e-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
# The mock org is local; make sure no ambient proxy intercepts the fetch.
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

# Neutralise any local webapp/.env before the app package is imported --
# otherwise a developer's real LLM connection leaks into the test run and
# results depend on a file that is not in the repository.
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests import mock_salesforce  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_mock():
    port = _free_port()
    config = uvicorn.Config(mock_salesforce.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port, server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("mock Salesforce did not start")


def _wait_done(client, org_id, timeout=60):
    """TestClient runs background tasks synchronously after the response, so
    this is normally already 'done' on the first poll -- the loop is here for
    safety, not because it is expected to spin."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/orgs/{org_id}/status").json()
        if s.get("status") in ("done", "error", "unknown"):
            return s
        time.sleep(0.2)
    return {"status": "timeout"}


def main():
    port, _server = _start_mock()
    instance_url = f"http://127.0.0.1:{port}"
    print(f"\nmock Salesforce on {instance_url}")

    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})

        print("\n-- first connection --")
        r = c.post("/api/orgs", json={
            "org_id": "mockorg", "org_name": "Mock Org", "instance_url": instance_url,
            "access_token": mock_salesforce.EXPECTED_TOKEN, "visibility": "private",
            "account": "Mock Customer"})
        check("connect accepted", r.status_code == 200, r.text[:200])
        s = _wait_done(c, "mockorg")
        check("first fetch completed", s.get("status") == "done", str(s)[:300])
        ch = s.get("changes") or {}
        check("reported as a first connection, not a refresh", ch.get("first_connection") is True, str(ch))
        first_total = ch.get("total", 0)
        check("indexed some components", first_total > 0, str(ch))

        check("account from the Connect form is on record",
              storage.load_registry()["mockorg"].get("account") == "Mock Customer")
        # Moved while nothing is running; the refreshes below must keep it.
        r = c.patch("/api/orgs/mockorg/account", json={"account": "Moved Customer"})
        check("account can be changed after connecting", r.status_code == 200, r.text[:200])

        print("\n-- refresh with nothing changed in the org --")
        r = c.post("/api/orgs/mockorg/refresh",
                   json={"access_token": mock_salesforce.EXPECTED_TOKEN})
        check("refresh accepted with only a token", r.status_code == 200, r.text[:200])
        check("reused the stored instance URL", r.json().get("reused_instance_url") is True, r.text[:200])
        s = _wait_done(c, "mockorg")
        check("refresh completed", s.get("status") == "done", str(s)[:300])
        ch = s.get("changes") or {}
        check("no components reported as changed",
              (ch.get("changed"), ch.get("added"), ch.get("removed")) == (0, 0, 0), str(ch))
        check("everything counted as unchanged", ch.get("unchanged") == first_total, str(ch))
        check("not mislabelled as a first connection", ch.get("first_connection") is False, str(ch))

        print("\n-- refresh after editing one class in the org --")
        edited = _edit_one_class()
        check("a class body was mutated in the mock org", edited is not None, "no class store found")
        if edited:
            r = c.post("/api/orgs/mockorg/refresh",
                       json={"access_token": mock_salesforce.EXPECTED_TOKEN})
            check("refresh accepted", r.status_code == 200, r.text[:160])
            s = _wait_done(c, "mockorg")
            ch = s.get("changes") or {}
            check("exactly one component reported changed", ch.get("changed") == 1, str(ch))
            check("the changed component is named", any(edited in k for k in ch.get("changed_sample", [])),
                  f"edited={edited} sample={ch.get('changed_sample')}")
            check("nothing spuriously added or removed",
                  (ch.get("added"), ch.get("removed")) == (0, 0), str(ch))

        print("\n-- the refresh did not disturb ownership or visibility --")
        reg = storage.load_registry()["mockorg"]
        check("owner preserved", reg.get("owner") == "admin", str(reg.get("owner")))
        check("visibility preserved", reg.get("visibility") == "private", str(reg.get("visibility")))
        check("account preserved across refreshes", reg.get("account") == "Moved Customer", str(reg.get("account")))
        check("summary persisted for the UI's 'last refresh' hint",
              isinstance(reg.get("last_refresh_changes"), dict), str(reg.get("last_refresh_changes"))[:120])

        print("\n-- the knowledgebase is still queryable afterwards --")
        stats = c.get("/api/orgs/mockorg/stats")
        check("stats still load", stats.status_code == 200, stats.text[:160])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All refresh end-to-end checks passed.")
    return 0


def _edit_one_class():
    """Change one Apex class body in the mock org so exactly one content hash
    moves. The mock serves its bodies from module-level constants, read at
    request time, so rebinding the module attribute is enough -- no restart.
    Returns the class NAME the refresh should report as changed."""
    body = getattr(mock_salesforce, "BUGGY_CLASS_BODY", None)
    if isinstance(body, str):
        mock_salesforce.BUGGY_CLASS_BODY = body + "\n// touched by test_refresh_e2e\n"
        return "MockPricingCallback"
    return None


def test_refresh_e2e():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
