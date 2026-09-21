"""
End-to-end check of per-org public/private visibility (app/org_access.py).

Runs the real FastAPI app against a temporary data directory, with three
accounts (alice/bob, both `user` role, plus the bootstrap admin) and a
knowledgebase written straight to disk -- no Salesforce and no onboarding
job needed, since visibility is enforced at the registry level.

Run:  python -m pytest tests/test_org_visibility.py -q
 or:  python tests/test_org_visibility.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point storage at a scratch dir BEFORE the app imports it.
_TMP = tempfile.mkdtemp(prefix="ts-vis-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"

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

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import auth, org_access  # noqa: E402
from app.common_now import iso_now  # noqa: E402


def _seed_org(org_id, owner, visibility):
    """Write a registry entry + a minimal knowledgebase, as a finished
    onboarding would."""
    reg = storage.load_registry()
    reg[org_id] = {
        "name": f"{org_id} name", "instance_url": "https://example.my.salesforce.com",
        "owner": owner, "visibility": visibility,
        "first_onboarded_at": iso_now(), "last_extracted_at": iso_now(),
        "component_counts": {"apex_classes": 1}, "warnings": [],
    }
    storage.save_registry(reg)
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_index.json"),
                       {"SomeClass": {"type": "ApexClass", "is_customer_authored": True}})
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_stats.json"),
                       {"counts": {"apex_classes": 1}})


def _seed_legacy_org(org_id):
    """An org connected before this feature existed: no owner, no visibility."""
    reg = storage.load_registry()
    reg[org_id] = {"name": "legacy", "instance_url": "https://legacy.my.salesforce.com",
                   "component_counts": {}, "warnings": []}
    storage.save_registry(reg)
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_index.json"), {"C": {}})
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_stats.json"), {"counts": {}})


def client_for(username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


FAILURES = []


def check(label, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{status}] {label}" + (f"  {extra}" if extra and not condition else ""))


def main():
    with TestClient(app) as boot:  # triggers startup -> bootstrap_admin
        admin = boot
        r = admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        assert r.status_code == 200, r.text
        for name in ("alice", "bob"):
            r = admin.post("/api/admin/users",
                           json={"username": name, "password": "password123", "role": "user"})
            assert r.status_code == 200, r.text
        r = admin.post("/api/admin/users",
                       json={"username": "ray", "password": "password123", "role": "reader"})
        assert r.status_code == 200, r.text

        alice = client_for("alice", "password123")
        bob = client_for("bob", "password123")
        ray = client_for("ray", "password123")

        _seed_org("alice_private", "alice", "private")
        _seed_org("alice_public", "alice", "public")
        _seed_legacy_org("legacy_org")

        print("\n-- listing --")
        a_list = alice.get("/api/orgs").json()
        b_list = bob.get("/api/orgs").json()
        adm_list = admin.get("/api/orgs").json()
        check("owner sees her own private org", "alice_private" in a_list)
        check("other user does NOT see it", "alice_private" not in b_list, str(list(b_list)))
        check("other user sees the public org", "alice_public" in b_list)
        check("admin sees the private org", "alice_private" in adm_list)
        check("legacy (ownerless) org stays visible to everyone", "legacy_org" in b_list)
        check("legacy org reports visibility=public",
              a_list["legacy_org"]["visibility"] == "public")
        check("list decorates owner", a_list["alice_public"]["owner"] == "alice")
        check("owner can manage her org", a_list["alice_public"]["can_manage"] is True)
        check("non-owner cannot manage it", b_list["alice_public"]["can_manage"] is False)
        check("non-owner cannot manage the legacy org", b_list["legacy_org"]["can_manage"] is False)
        check("admin can manage the legacy org", adm_list["legacy_org"]["can_manage"] is True)

        print("\n-- reading a private org --")
        check("owner reads stats", alice.get("/api/orgs/alice_private/stats").status_code == 200)
        check("admin reads stats", admin.get("/api/orgs/alice_private/stats").status_code == 200)
        r = bob.get("/api/orgs/alice_private/stats")
        check("non-owner gets 404 (not 403 -- existence is hidden)", r.status_code == 404, r.text)
        for path in ("search?q=x", "components/SomeClass", "object-touch/Account",
                     "field-writers/F__c", "inbound/SomeClass", "entry-points/Account",
                     "incidents", "known-issues", "status", "visibility"):
            r = bob.get(f"/api/orgs/alice_private/{path}")
            check(f"non-owner blocked on GET {path}", r.status_code == 404, str(r.status_code))

        print("\n-- reading a public org --")
        check("non-owner reads public stats", bob.get("/api/orgs/alice_public/stats").status_code == 200)
        check("reader reads public stats", ray.get("/api/orgs/alice_public/stats").status_code == 200)
        check("reader blocked on private org", ray.get("/api/orgs/alice_private/stats").status_code == 404)

        print("\n-- writing --")
        r = bob.post("/api/orgs/alice_public/incidents", data={"field": "Some__c"})
        check("non-owner with 'user' role CAN file an incident on a public org",
              r.status_code == 200, r.text[:200])
        r = ray.post("/api/orgs/alice_public/incidents", data={"field": "Some__c"})
        check("reader still cannot file an incident (role gate intact)", r.status_code == 403, r.text[:120])
        r = bob.post("/api/orgs/alice_private/incidents", data={"field": "Some__c"})
        check("non-owner cannot file on a private org", r.status_code == 404)

        print("\n-- managing visibility --")
        r = bob.patch("/api/orgs/alice_public/visibility", json={"visibility": "private"})
        check("non-owner cannot flip a public org private", r.status_code == 403, r.text[:120])
        r = bob.patch("/api/orgs/alice_private/visibility", json={"visibility": "public"})
        check("non-owner gets 404 for a private org he can't see", r.status_code == 404)
        r = alice.patch("/api/orgs/alice_private/visibility", json={"visibility": "public"})
        check("owner can publish her org", r.status_code == 200, r.text[:120])
        check("now visible to the other user", "alice_private" in bob.get("/api/orgs").json())
        r = alice.patch("/api/orgs/alice_private/visibility", json={"visibility": "private"})
        check("owner can make it private again", r.status_code == 200)
        check("and it disappears again", "alice_private" not in bob.get("/api/orgs").json())
        r = admin.patch("/api/orgs/alice_public/visibility", json={"visibility": "private"})
        check("admin can change someone else's org", r.status_code == 200, r.text[:120])
        admin.patch("/api/orgs/alice_public/visibility", json={"visibility": "public"})
        r = alice.patch("/api/orgs/alice_public/visibility", json={"visibility": "sideways"})
        check("bad visibility value is rejected", r.status_code == 400, r.text[:120])
        r = admin.patch("/api/orgs/legacy_org/visibility", json={"visibility": "private"})
        check("admin adopting a legacy org stamps ownership",
              r.status_code == 200 and r.json().get("owner") == "admin", r.text[:120])
        check("legacy org now hidden from others",
              "legacy_org" not in bob.get("/api/orgs").json())

        print("\n-- re-connect / refresh is owner-only --")
        body = {"org_id": "alice_public", "org_name": "x", "instance_url": "https://x.example.com",
                "access_token": "tok"}
        r = bob.post("/api/orgs", json=body)
        check("non-owner cannot re-connect a public org he can see", r.status_code == 403, r.text[:140])
        r = bob.post("/api/orgs/alice_public/refresh", json={"access_token": "tok"})
        check("non-owner cannot refresh it", r.status_code == 403, str(r.status_code))
        r = bob.post("/api/orgs/alice_private/refresh", json={"access_token": "tok"})
        check("non-owner refreshing a hidden org gets 404", r.status_code == 404, str(r.status_code))
        r = alice.post("/api/orgs/alice_public/refresh", json={"access_token": "tok"})
        check("owner can refresh with just a token", r.status_code == 200, r.text[:140])
        r = bob.post("/api/orgs", json={**body, "org_id": "alice_private"})
        check("non-owner re-connecting a hidden org gets 404, not 'already exists'",
              r.status_code == 404, r.text[:140])

        print("\n-- defaults --")
        reg = storage.load_registry()
        check("new orgs default to private",
              org_access.normalize_visibility(None) == "private")
        check("registry keeps owner across a refresh-shaped write",
              reg["alice_public"].get("owner") == "alice")

        # A brand-new org queued by bob: ownership must be stamped at queue
        # time so /status is gated even before the registry entry exists.
        r = bob.post("/api/orgs", json={"org_id": "bob_new", "org_name": "Bob New",
                                        "instance_url": "https://nope.invalid", "access_token": "t"})
        check("queueing a new org reports owner + default visibility",
              r.status_code == 200 and r.json().get("owner") == "bob"
              and r.json().get("visibility") == "private", r.text[:160])
        check("another user cannot poll status of an in-flight private org",
              alice.get("/api/orgs/bob_new/status").status_code == 404)
        check("owner can poll status of her in-flight org",
              bob.get("/api/orgs/bob_new/status").status_code == 200)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("All visibility checks passed.")
    return 0


def test_org_visibility():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
