"""
Covers the endpoints added for the UX pass:

  POST /api/orgs/{id}/refresh   -- one-click re-fetch (owner/admin only, reuses
                                   the stored instance URL, reports what changed)
  POST /api/auth/password       -- change your own password

plus onboarding.diff_hashes, which produces the "3 changed, 1 new" summary the
UI shows after a refresh.

Run:  python tests/test_refresh_and_password.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-refresh-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"

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
from app import onboarding  # noqa: E402
from app.common_now import iso_now  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _seed_org(org_id, owner, visibility="private", instance_url="https://acme.my.salesforce.com"):
    reg = storage.load_registry()
    reg[org_id] = {"name": "Acme Prod", "instance_url": instance_url, "owner": owner,
                   "visibility": visibility, "first_onboarded_at": iso_now(),
                   "last_extracted_at": iso_now(), "component_counts": {}, "warnings": []}
    storage.save_registry(reg)
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_index.json"), {"C": {}})


def client_for(c, username, password):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def main():
    print("\n-- diff_hashes (the 'what changed' summary) --")
    before = {"classes/A.cls": {"hash": "1"}, "classes/B.cls": {"hash": "2"},
              "flows/F.flow": {"hash": "3"}}
    after = {"classes/A.cls": {"hash": "1"},        # unchanged
             "classes/B.cls": {"hash": "CHANGED"},  # changed
             "flows/F.flow": {"hash": "3"},         # unchanged
             "triggers/T.trigger": {"hash": "9"}}   # new
    d = onboarding.diff_hashes(before, after)
    check("counts changed/added/removed/unchanged",
          (d["changed"], d["added"], d["removed"], d["unchanged"]) == (1, 1, 0, 2), str(d))
    check("per-kind breakdown", d["changed_by_kind"] == {"classes": 1}
          and d["added_by_kind"] == {"triggers": 1}, str(d))
    check("names a sample", d["changed_sample"] == ["classes/B.cls"], str(d))
    check("removal is detected", onboarding.diff_hashes(before, {})["removed"] == 3)
    check("an empty 'before' is a first connection, not a refresh",
          onboarding.diff_hashes({}, after)["first_connection"] is True)
    check("a real refresh is not flagged as first connection", d["first_connection"] is False)

    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        for name in ("alice", "bob"):
            r = admin.post("/api/admin/users",
                           json={"username": name, "password": "password123", "role": "user"})
            assert r.status_code == 200, r.text

        alice = client_for(TestClient(app), "alice", "password123")
        bob = client_for(TestClient(app), "bob", "password123")

        _seed_org("acme", "alice", "public")

        print("\n-- refresh --")
        r = bob.post("/api/orgs/acme/refresh", json={"access_token": "tok"})
        check("a non-owner cannot refresh a public org", r.status_code == 403, r.text[:140])
        r = alice.post("/api/orgs/acme/refresh", json={"access_token": "tok"})
        check("the owner can refresh with only a token", r.status_code == 200, r.text[:200])
        check("the stored instance URL is reused -- nothing to retype",
              r.json().get("instance_url") == "https://acme.my.salesforce.com"
              and r.json().get("reused_instance_url") is True, r.text[:200])
        r = admin.post("/api/orgs/acme/refresh", json={"access_token": "tok"})
        check("an admin can refresh someone else's org", r.status_code == 200, r.text[:140])
        r = alice.post("/api/orgs/acme/refresh", json={})
        check("a token is required", r.status_code == 422, str(r.status_code))
        r = alice.post("/api/orgs/nope/refresh", json={"access_token": "t"})
        check("refreshing an org that doesn't exist is a 404", r.status_code == 404)

        _seed_org("hidden", "alice", "private")
        r = bob.post("/api/orgs/hidden/refresh", json={"access_token": "t"})
        check("refreshing a private org you can't see is 404, not 403", r.status_code == 404)

        # An org whose registry entry has no instance_url must say so clearly
        # rather than firing a fetch at an empty URL.
        reg = storage.load_registry()
        reg["acme"]["instance_url"] = ""
        storage.save_registry(reg)
        r = alice.post("/api/orgs/acme/refresh", json={"access_token": "t"})
        check("a missing stored instance URL is explained, not silently attempted",
              r.status_code == 400 and "instance" in r.text.lower(), r.text[:140])
        r = alice.post("/api/orgs/acme/refresh",
                       json={"access_token": "t", "instance_url": "https://new.my.salesforce.com"})
        check("...and can be supplied inline", r.status_code == 200, r.text[:140])

        print("\n-- refresh preserves ownership and visibility --")
        reg = storage.load_registry()
        check("owner unchanged by a refresh", reg["acme"].get("owner") == "alice")
        check("visibility unchanged by a refresh", reg["acme"].get("visibility") == "public")

        print("\n-- change your own password --")
        r = bob.post("/api/auth/password",
                     json={"current_password": "wrongpass", "new_password": "newpassword1"})
        check("the current password is required to be correct", r.status_code == 403, r.text[:120])
        r = bob.post("/api/auth/password",
                     json={"current_password": "password123", "new_password": "short"})
        check("a too-short new password is rejected", r.status_code == 400, r.text[:120])
        r = bob.post("/api/auth/password",
                     json={"current_password": "password123", "new_password": "newpassword1"})
        check("bob can change his own password", r.status_code == 200, r.text[:120])
        fresh = TestClient(app)
        check("the old password no longer works",
              fresh.post("/api/auth/login", json={"username": "bob", "password": "password123"}).status_code == 401)
        check("the new password works",
              fresh.post("/api/auth/login", json={"username": "bob", "password": "newpassword1"}).status_code == 200)

        anon = TestClient(app)
        r = anon.post("/api/auth/password",
                      json={"current_password": "x", "new_password": "yyyyyyyy"})
        check("an unauthenticated caller cannot change a password", r.status_code == 401)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All refresh / password checks passed.")
    return 0


def test_refresh_and_password():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
