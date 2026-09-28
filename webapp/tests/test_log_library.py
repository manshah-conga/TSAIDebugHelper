"""
Log library (app/log_library.py): owner tracking, org/account tags, search,
visibility inherited from a tagged org, archive/unarchive, retag, delete --
end to end through the real FastAPI app on a scratch data directory.

Run:  python tests/test_log_library.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-loglib-test-")
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
from app.common_now import iso_now  # noqa: E402

FAILURES = []

LOG = """64.0 APEX_CODE,FINEST;DB,INFO
12:00:00.0 (1)|EXECUTION_STARTED
12:00:00.0 (2)|CODE_UNIT_STARTED|[EXTERNAL]|01q000000000001|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (3)|EXCEPTION_THROWN|[12]|System.NullPointerException: Attempt to de-reference a null object
12:00:00.0 (4)|FATAL_ERROR|System.NullPointerException: Attempt to de-reference a null object

Trigger.QuoteTrigger: line 12, column 1
12:00:00.0 (5)|CODE_UNIT_FINISHED|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (6)|EXECUTION_FINISHED
"""


def check(label, condition, extra=""):
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))


def seed(org_id, owner, visibility, account=None):
    reg = storage.load_registry()
    reg[org_id] = {"name": f"{org_id} name", "instance_url": f"https://{org_id}.my.salesforce.com",
                   "owner": owner, "visibility": visibility, "first_onboarded_at": iso_now(),
                   "last_extracted_at": iso_now(), "component_counts": {}, "warnings": []}
    if account:
        reg[org_id]["account"] = account
    storage.save_registry(reg)


def login(u, p):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return c


def upload(client, label=None, org_id=None, account=None, store=True, name="debug.log"):
    data = {"store": "true" if store else "false"}
    if label:
        data["label"] = label
    if org_id:
        data["org_id"] = org_id
    if account:
        data["account"] = account
    return client.post("/api/logs/normalize", data=data,
                       files={"log_file": (name, LOG.encode(), "text/plain")})


def ids(resp):
    return [m["log_id"] for m in resp.json()]


def main():
    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        for n, role in (("alice", "user"), ("bob", "user"), ("rita", "reader")):
            r = admin.post("/api/admin/users", json={"username": n, "password": "password123", "role": role})
            assert r.status_code in (200, 201), r.text

        seed("acmeprod", "alice", "public", account="Acme Corp")
        seed("acmeuat", "alice", "private")        # private, no account
        seed("globex", "bob", "public", account="Globex")

        alice, bob, rita = login("alice", "password123"), login("bob", "password123"), login("rita", "password123")

        print("\n-- build handshake --")
        from app import main as _m
        b = TestClient(app).get("/api/build")
        check("build is served without a session", b.status_code == 200 and b.json()["build"] == _m.APP_BUILD)
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "app.js"), encoding="utf-8").read()
        html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "index.html"), encoding="utf-8").read()
        check("page build matches server build", f"const CLIENT_BUILD = {_m.APP_BUILD};" in js)
        check("asset ?v= matches the build", f"app.js?v={_m.APP_BUILD}" in html)

        print("\n-- upload + tags --")
        r = upload(alice, label="quote npe", org_id="acmeprod", account="Something Else")
        check("store with org tag", r.status_code == 200, r.text)
        m1 = r.json()["meta"]
        check("owner recorded", m1["owner"] == "alice")
        check("org tag recorded", m1["org_id"] == "acmeprod")
        check("org's account wins over the typed one", m1["account"] == "Acme Corp", m1.get("account"))
        check("caller can manage their own log", m1["can_manage"] is True)

        r = upload(alice, label="uat run", org_id="acmeuat")
        m2 = r.json()["meta"]
        check("private-org tag ok for the org owner", r.status_code == 200 and m2["org_id"] == "acmeuat")

        r = upload(bob, label="sneaky", org_id="acmeuat")
        check("cannot tag an org you cannot see (404)", r.status_code == 404, r.status_code)

        r = upload(bob, label="acme from bob", account="acme corp")
        m3 = r.json()["meta"]
        check("account-only tag snaps to existing spelling", m3["account"] == "Acme Corp", m3.get("account"))
        check("no org tag on account-only log", m3["org_id"] is None)

        r = upload(bob, label="untagged")
        m4 = r.json()["meta"]
        check("untagged log stores fine", r.status_code == 200 and m4["account"] is None)

        r = upload(alice, label="quote npe", org_id="acmeprod")
        m5 = r.json()["meta"]
        check("same label never overwrites an earlier log", m5["log_id"] != m1["log_id"]
              and storage.read_log_meta(m1["log_id"]) is not None)
        from app import main as main_mod
        nxt = main_mod._unique_log_id(m1["log_id"])
        check("id collision gets a suffix", nxt != m1["log_id"] and nxt.startswith(m1["log_id"] + "_") and storage.read_log_meta(nxt) is None, nxt)

        r = upload(rita, label="reader")
        check("reader cannot store", r.status_code == 403, r.status_code)

        r = upload(alice, store=False)
        check("normalize without storing still works", r.status_code == 200 and r.json()["stored"] is False)

        r = alice.post("/api/logs/normalize", data={"store": "true", "account": "x" * 200},
                       files={"log_file": ("a.log", LOG.encode(), "text/plain")})
        check("over-long account rejected", r.status_code == 400, r.status_code)

        print("\n-- visibility --")
        a_ids, b_ids, r_ids = ids(alice.get("/api/logs")), ids(bob.get("/api/logs")), ids(rita.get("/api/logs"))
        check("owner sees their private-org log", m2["log_id"] in a_ids)
        check("others do not see a private-org log", m2["log_id"] not in b_ids and m2["log_id"] not in r_ids)
        check("admin sees it", m2["log_id"] in ids(admin.get("/api/logs")))
        check("private-org log 404s for others", bob.get(f"/api/logs/{m2['log_id']}").status_code == 404)
        check("...and its download too", bob.get(f"/api/logs/{m2['log_id']}/download").status_code == 404)
        check("public-org log visible to reader", m1["log_id"] in r_ids)
        check("untagged log visible to all", m4["log_id"] in a_ids and m4["log_id"] in r_ids)

        print("\n-- search / filters --")
        check("filter by account",
              set(ids(rita.get("/api/logs", params={"account": "ACME corp"}))) == {m1["log_id"], m3["log_id"], m5["log_id"]},
              ids(rita.get("/api/logs", params={"account": "ACME corp"})))
        check("filter by org", set(ids(rita.get("/api/logs", params={"org_id": "acmeprod"}))) == {m1["log_id"], m5["log_id"]})
        check("filter unassigned", ids(rita.get("/api/logs", params={"account": "__unassigned__"})) == [m4["log_id"]])
        check("q matches account text", m3["log_id"] in ids(rita.get("/api/logs", params={"q": "acme"})))
        check("q matches org id", m1["log_id"] in ids(rita.get("/api/logs", params={"q": "acmeprod"})))
        check("q terms are ANDed", ids(rita.get("/api/logs", params={"q": "acme bob"})) == [m3["log_id"]],
              ids(rita.get("/api/logs", params={"q": "acme bob"})))
        check("q matches exception", len(ids(rita.get("/api/logs", params={"q": "nullpointer"}))) >= 4)
        check("owner=me", set(ids(bob.get("/api/logs", params={"owner": "me"}))) == {m3["log_id"], m4["log_id"]})
        check("search never reveals private-org log",
              m2["log_id"] not in ids(bob.get("/api/logs", params={"q": "acmeuat"})))
        f = rita.get("/api/logs/facets").json()
        check("facets list accounts", {a["account"] for a in f["accounts"]} >= {"Acme Corp", None})
        check("facets hide private org", "acmeuat" not in {o["org_id"] for o in f["orgs"]})
        check("bad status rejected", rita.get("/api/logs", params={"status": "nope"}).status_code == 400)

        print("\n-- account follows the org --")
        admin.patch("/api/orgs/acmeprod/account", json={"account": "ACME Holdings"})
        got = {m["log_id"]: m for m in rita.get("/api/logs").json()}
        check("org move carries its logs", got[m1["log_id"]]["account"] == "ACME Holdings", got[m1["log_id"]]["account"])
        check("account-only log keeps its own account", got[m3["log_id"]]["account"] == "Acme Corp")

        print("\n-- archive / retag / delete permissions --")
        r = bob.patch(f"/api/logs/{m1['log_id']}", json={"archived": True})
        check("non-owner cannot archive (403)", r.status_code == 403, r.status_code)
        r = rita.patch(f"/api/logs/{m4['log_id']}", json={"archived": True})
        check("reader cannot archive (403)", r.status_code == 403, r.status_code)
        r = bob.patch(f"/api/logs/{m2['log_id']}", json={"archived": True})
        check("invisible log is 404 not 403", r.status_code == 404, r.status_code)

        r = alice.patch(f"/api/logs/{m1['log_id']}", json={"archived": True})
        check("owner archives", r.status_code == 200 and r.json()["archived"] is True, r.text)
        check("archived_by stamped", r.json().get("archived_by") == "alice")
        check("archived hidden by default", m1["log_id"] not in ids(alice.get("/api/logs")))
        check("status=archived shows it", ids(alice.get("/api/logs", params={"status": "archived"})) == [m1["log_id"]])
        check("status=all includes it", m1["log_id"] in ids(alice.get("/api/logs", params={"status": "all"})))
        check("archived log still opens", alice.get(f"/api/logs/{m1['log_id']}").status_code == 200)
        r = admin.patch(f"/api/logs/{m1['log_id']}", json={"archived": False})
        check("admin unarchives someone else's log", r.status_code == 200 and r.json()["archived"] is False)

        r = bob.patch(f"/api/logs/{m4['log_id']}", json={"org_id": "globex", "label": "  renamed   log "})
        check("owner retags to an org", r.status_code == 200 and r.json()["org_id"] == "globex", r.text)
        check("retag picks up org's account", r.json()["account"] == "Globex")
        check("label cleaned", r.json()["label"] == "renamed log")
        r = bob.patch(f"/api/logs/{m4['log_id']}", json={"org_id": "acmeuat"})
        check("cannot retag to an invisible org", r.status_code == 404, r.status_code)
        r = bob.patch(f"/api/logs/{m4['log_id']}", json={"org_id": None, "account": None})
        check("clear both tags", r.status_code == 200 and r.json()["org_id"] is None and r.json()["account"] is None, r.text)
        r = bob.patch(f"/api/logs/{m4['log_id']}", json={})
        check("empty patch rejected", r.status_code == 400)

        # A legacy log (no owner) -- admin-only to manage.
        storage.save_normalized_log("20250101T000000Z_legacy", {"exceptions": []},
                                    {"log_id": "20250101T000000Z_legacy", "timestamp": "2025-01-01T00:00:00Z",
                                     "exception_count": 0, "top_exception": None})
        legacy = [m for m in alice.get("/api/logs").json() if m["log_id"] == "20250101T000000Z_legacy"][0]
        check("legacy log visible, owner null", legacy["owner"] is None and legacy["can_manage"] is False)
        check("user cannot delete legacy log", alice.delete("/api/logs/20250101T000000Z_legacy").status_code == 403)
        check("admin can delete legacy log", admin.delete("/api/logs/20250101T000000Z_legacy").status_code == 200)

        r = bob.delete(f"/api/logs/{m1['log_id']}")
        check("non-owner cannot delete", r.status_code == 403)
        r = alice.delete(f"/api/logs/{m1['log_id']}")
        check("owner deletes", r.status_code == 200 and r.json()["deleted"] is True)
        check("deleted log is gone", alice.get(f"/api/logs/{m1['log_id']}").status_code == 404)
        check("deleted dir removed from disk", not os.path.isdir(os.path.join(storage.LOGS_ROOT, m1["log_id"])))
        r = admin.delete(f"/api/logs/{m3['log_id']}")
        check("admin deletes another user's log", r.status_code == 200)

        print("\n-- id hygiene --")
        check("traversal-looking id is 404", alice.get("/api/logs/..").status_code in (404, 405))
        check("dotted id refused", alice.delete("/api/logs/a.b").status_code == 404)

        print("\n-- nothing raw stored --")
        blob = ""
        for root, _, files in os.walk(storage.LOGS_ROOT):
            for fn in files:
                with open(os.path.join(root, fn), encoding="utf-8") as fh:
                    blob += fh.read()
        check("raw header line not stored", "APEX_CODE,FINEST;DB,INFO" not in blob)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("All log-library checks passed.")


if __name__ == "__main__":
    main()
