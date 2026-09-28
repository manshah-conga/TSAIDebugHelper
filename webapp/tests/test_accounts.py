"""
Customer-account grouping of orgs (app/accounts.py), end to end through the
real FastAPI app on a scratch data directory.

Run:  python tests/test_accounts.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-acct-test-")
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
from app import accounts, onboarding  # noqa: E402
from app.common_now import iso_now  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))


def seed(org_id, owner, visibility, url, account=None):
    reg = storage.load_registry()
    reg[org_id] = {"name": f"{org_id} name", "instance_url": url, "owner": owner,
                   "visibility": visibility, "first_onboarded_at": iso_now(),
                   "last_extracted_at": iso_now(), "component_counts": {}, "warnings": []}
    if account:
        reg[org_id]["account"] = account
    storage.save_registry(reg)


def login(u, p):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
    return c


def main():
    print("\n-- URL heuristics --")
    check("prod my domain", accounts.my_domain("https://acme.my.salesforce.com") == "acme")
    check("sandbox shares prod my domain",
          accounts.my_domain("https://acme--uat.sandbox.my.salesforce.com/") == "acme")
    check("legacy instance has none", accounts.my_domain("https://na42.salesforce.com") is None)
    check("env: production", accounts.environment("https://acme.my.salesforce.com") == "production")
    check("env: sandbox", accounts.environment("acme--uat.sandbox.my.salesforce.com") == "sandbox")
    check("env: developer", accounts.environment("https://x-dev-ed.develop.my.salesforce.com") == "developer")
    check("env: scratch", accounts.environment("https://y.scratch.my.salesforce.com") == "scratch")
    check("env: unknown", accounts.environment("") == "unknown")
    check("names: whitespace collapsed", accounts.normalize_account("  Acme   Corp ") == "Acme Corp")
    check("names: empty is unassigned", accounts.normalize_account("   ") is None)
    try:
        accounts.normalize_account("x" * 200); ok = False
    except ValueError:
        ok = True
    check("names: too long rejected", ok)

    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        for n in ("alice", "bob"):
            admin.post("/api/admin/users", json={"username": n, "password": "password123", "role": "user"})
        alice, bob = login("alice", "password123"), login("bob", "password123")

        seed("acme_prod", "alice", "public", "https://acme.my.salesforce.com", "Acme")
        seed("acme_uat", "alice", "private", "https://acme--uat.sandbox.my.salesforce.com")
        seed("bob_acme_dev", "bob", "public", "https://acme--dev.sandbox.my.salesforce.com", "Acme")
        seed("secret_org", "bob", "private", "https://globex.my.salesforce.com", "Globex Secret")

        print("\n-- listing --")
        orgs = alice.get("/api/orgs").json()
        check("org list carries account", orgs["acme_prod"]["account"] == "Acme")
        check("...and derived environment", orgs["acme_uat"]["environment"] == "sandbox"
              and orgs["acme_prod"]["environment"] == "production")
        check("unassigned reads as null", orgs["acme_uat"]["account"] is None)
        accs = alice.get("/api/accounts").json()["accounts"]
        names = [a["account"] for a in accs]
        check("accounts sorted, unassigned last", names == ["Acme", None], str(names))
        check("a private org's account is not listed to others", "Globex Secret" not in names)
        acme = accs[0]
        check("account counts environments", acme["environments"] == {"production": 1, "sandbox": 1}, str(acme))
        check("can_manage_all false when a colleague's org is in it", acme["can_manage_all"] is False)

        print("\n-- suggestions --")
        s = alice.get("/api/accounts/suggest",
                      params={"instance_url": "https://acme--full.sandbox.my.salesforce.com"}).json()
        check("sibling on same My Domain supplies the account", s["matched_account"] == "Acme", str(s))
        check("...and says it's a sandbox", s["environment"] == "sandbox")
        s = alice.get("/api/accounts/suggest",
                      params={"instance_url": "https://globex--qa.sandbox.my.salesforce.com"}).json()
        check("a private sibling's account is not leaked", s["matched_account"] is None and s["suggestion"] == "globex", str(s))

        print("\n-- moving an org --")
        r = alice.patch("/api/orgs/acme_uat/account", json={"account": "  acme "})
        check("owner can move her org", r.status_code == 200, r.text)
        check("name snaps to existing spelling", r.json()["account"] == "Acme", r.text)
        r = bob.patch("/api/orgs/acme_prod/account", json={"account": "Stolen"})
        check("non-owner cannot move a public org", r.status_code == 403, r.text)
        r = bob.patch("/api/orgs/acme_uat/account", json={"account": "X"})
        check("...and a private one is 404", r.status_code == 404, r.text)
        r = alice.patch("/api/orgs/acme_uat/account", json={"account": "x" * 200})
        check("bad name is a 400", r.status_code == 400)
        r = alice.patch("/api/orgs/acme_uat/account", json={"account": None})
        check("null unassigns", r.status_code == 200 and "account" not in storage.load_registry()["acme_uat"])
        alice.patch("/api/orgs/acme_uat/account", json={"account": "Acme"})

        print("\n-- renaming --")
        r = alice.post("/api/accounts/rename", json={"from_account": "acme", "to_account": "Acme Corp"})
        check("rename refused when a visible org is someone else's", r.status_code == 403, r.text)
        check("...and says which", "bob_acme_dev" in r.text)
        r = admin.post("/api/accounts/rename", json={"from_account": "ACME", "to_account": "Acme Corp"})
        check("admin rename covers every org", r.status_code == 200 and r.json()["orgs"] ==
              ["acme_prod", "acme_uat", "bob_acme_dev"], r.text)
        reg = storage.load_registry()
        check("...persisted", {reg[o].get("account") for o in ("acme_prod", "acme_uat", "bob_acme_dev")} == {"Acme Corp"})
        r = admin.post("/api/accounts/rename", json={"from_account": "Acme Corp", "to_account": "globex secret"})
        check("rename into an existing account merges with its spelling",
              r.status_code == 200 and r.json()["to"] == "Globex Secret", r.text)
        r = admin.post("/api/accounts/rename", json={"from_account": "nope", "to_account": "x"})
        check("unknown account is 404", r.status_code == 404)

        print("\n-- create / onboarding preserve the account --")
        seed("acme_prod", "alice", "public", "https://acme.my.salesforce.com", "Acme")
        reg = storage.load_registry(); reg["acme_uat"]["account"] = "Acme"; storage.save_registry(reg)

        captured = {}

        async def fake_run(org_id, org_name, url, tok, existing, owner, vis, account=None):
            captured.update(org_id=org_id, account=account)
        real = onboarding.run_onboarding
        import app.main as main_mod
        main_mod.run_onboarding = fake_run
        try:
            r = alice.post("/api/orgs", json={"org_id": "acme_full", "org_name": "Acme Full",
                           "instance_url": "https://acme--full.sandbox.my.salesforce.com",
                           "access_token": "t"})
            check("new org inherits a visible sibling's account", r.json().get("account") == "Acme", r.text)
            r = alice.post("/api/orgs", json={"org_id": "initech", "org_name": "Initech",
                           "instance_url": "https://initech.my.salesforce.com", "access_token": "t",
                           "account": "Initech"})
            check("explicit account is passed to the fetch", captured.get("account") == "Initech", str(captured))
        finally:
            main_mod.run_onboarding = real

        # Account survival across a real refresh is covered end to end in
        # tests/test_refresh_e2e.py (mock Salesforce + the real onboarding write).

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("All account checks passed.")


if __name__ == "__main__":
    main()
