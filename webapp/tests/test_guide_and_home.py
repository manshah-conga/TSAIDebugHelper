"""
Home page + onboarding guide API (app/guide.py): per-user guide state, the
self-ticking checklist, role-specific items, the home summary, and free-text
known-issue matching -- including that matching respects org visibility.

Run:  python tests/test_guide_and_home.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-guide-test-")
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
from app import auth, rate_limit  # noqa: E402
from app import chat as chat_agent  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{status}] {label}" + (f"  {extra}" if extra and not condition else ""))


def login(username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def seed_org(org_id, owner, visibility, known):
    def _reg(reg):
        reg[org_id] = {"name": f"{org_id} name", "owner": owner, "visibility": visibility,
                       "instance_url": "https://x.invalid", "component_counts": {},
                       "last_extracted_at": "2026-09-01T00:00:00Z"}
    storage.mutate_registry(_reg)
    storage.save_known_issues(org_id, known)
    for iid in {i for v in known.values() for i in v.get("incident_ids", [])}:
        storage.save_incident(org_id, iid, {}, {}, {"incident_id": iid})


def ids(items):
    return [i["id"] for i in items]


def main():
    with TestClient(app):
        admin = login("admin", "adminpassword123")
        auth.create_user("wendy", "password123", "user", created_by="admin:admin")
        auth.create_user("rory", "password123", "reader", created_by="admin:admin")
        rate_limit.reset()
        wendy = login("wendy", "password123")
        rory = login("rory", "password123")

        print("\n-- a new account's guide --")
        g = wendy.get("/api/me/guide").json()
        check("a new writer is 'learning'", g["learning"] is True, str(g)[:200])
        check("...has not seen the welcome", g["welcome_seen"] is False)
        check("...and nothing on the checklist is done", g["checklist_done"] == 0, str(g["checklist"]))
        check("writer checklist includes connect/normalize/incident/fix",
              {"connect", "normalize", "incident", "fix"} <= set(ids(g["checklist"])))
        check("writer checklist has no admin item", "admin" not in ids(g["checklist"]))

        gr = rory.get("/api/me/guide").json()
        check("reader checklist has no write actions",
              not ({"connect", "normalize", "incident", "fix"} & set(ids(gr["checklist"]))),
              str(ids(gr["checklist"])))
        check("reader checklist has Known Issues instead", "known" in ids(gr["checklist"]))
        ga = admin.get("/api/me/guide").json()
        check("admin checklist has the admin item", "admin" in ids(ga["checklist"]))

        print("\n-- recording progress --")
        r = wendy.post("/api/me/guide", json={"event": "search"})
        check("an event is recorded", r.status_code == 200 and
              next(i for i in r.json()["checklist"] if i["id"] == "search")["done"])
        check("an unknown event is refused",
              wendy.post("/api/me/guide", json={"event": "rm -rf"}).status_code == 400)
        check("an unknown tour is refused",
              wendy.post("/api/me/guide", json={"tour_done": "nope"}).status_code == 400)
        check("an unknown tab is refused",
              wendy.post("/api/me/guide", json={"tab_seen": "../etc"}).status_code == 400)
        wendy.post("/api/me/guide", json={"tab_seen": "incidents"})
        wendy.post("/api/me/guide", json={"tab_seen": "incidents"})
        g = wendy.get("/api/me/guide").json()
        check("tabs are remembered once", g["tabs_seen"] == ["incidents"], str(g["tabs_seen"]))
        wendy.post("/api/me/guide", json={"visit": True})
        wendy.post("/api/me/guide", json={"visit": True})
        check("visits are counted", wendy.get("/api/me/guide").json()["visits"] == 2)
        wendy.post("/api/me/guide", json={"welcome_seen": True, "tour_done": "demo"})
        g = wendy.get("/api/me/guide").json()
        check("welcome + demo tour recorded", g["welcome_seen"] and "demo" in g["tours_done"])
        check("the demo tour ticks the checklist",
              next(i for i in g["checklist"] if i["id"] == "tour")["done"])
        check("one user's progress is not another's",
              rory.get("/api/me/guide").json()["checklist_done"] == 0)

        print("\n-- derived items --")
        r = wendy.post("/api/tokens", json={"label": "desktop"})
        check("token created", r.status_code == 200, r.text[:120])
        g = wendy.get("/api/me/guide").json()
        check("creating a token ticks 'token' without any browser event",
              next(i for i in g["checklist"] if i["id"] == "token")["done"])
        seed_org("wendy_org", "wendy", "private", {})
        g = wendy.get("/api/me/guide").json()
        check("owning an org ticks 'connect'",
              next(i for i in g["checklist"] if i["id"] == "connect")["done"])

        print("\n-- finishing, dismissing, resetting --")
        wendy.post("/api/me/guide", json={"checklist_dismissed": True})
        check("dismissing ends 'learning'", wendy.get("/api/me/guide").json()["learning"] is False)
        wendy.post("/api/me/guide", json={"reset": True})
        g = wendy.get("/api/me/guide").json()
        check("reset brings the guide back", g["learning"] is True and not g["welcome_seen"])
        check("...but keeps recorded events", next(i for i in g["checklist"] if i["id"] == "search")["done"])
        for ev in ("writers", "normalize", "incident", "fix", "ask"):
            wendy.post("/api/me/guide", json={"event": ev})
        wendy.post("/api/me/guide", json={"tour_done": "demo"})
        g = wendy.get("/api/me/guide").json()
        check("everything done -> complete", g["checklist_done"] == g["checklist_total"],
              str([i for i in g["checklist"] if not i["done"]]))
        check("...and no longer learning", g["learning"] is False)
        check("...completion is stamped", bool(g["checklist_completed_at"]))

        print("\n-- preferences --")
        r = wendy.post("/api/me/guide", json={"prefs": {"pinned_orgs": ["wendy_org"], "org_view": "table",
                                                         "connect_open": False, "evil": 1}})
        p = r.json()["prefs"]
        check("prefs saved", p.get("pinned_orgs") == ["wendy_org"] and p.get("org_view") == "table"
              and p.get("connect_open") is False, str(p))
        check("unknown pref keys are dropped", "evil" not in p)
        check("a bad org_view is refused",
              wendy.post("/api/me/guide", json={"prefs": {"org_view": "grid"}}).status_code == 400)
        r = wendy.post("/api/me/guide", json={"prefs": {"connect_open": None}})
        check("null resets one pref", "connect_open" not in r.json()["prefs"]
              and r.json()["prefs"].get("org_view") == "table")

        print("\n-- home summary + visibility --")
        npe = {"kind": "exception", "type": "System.NullPointerException",
               "message_sample": "Attempt to de-reference a null object",
               "stack_sample": ["Class.AgreementHelper.share: line 62, column 1"],
               "first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-02T00:00:00Z",
               "occurrences": 2, "incident_ids": ["20260901T000000Z_a", "20260902T000000Z_b"],
               "resolution": "Flow Set_Region skipped non-US accounts; restored the default path.",
               "resolution_recorded_at": "2026-09-03T00:00:00Z"}
        lock = {"kind": "exception", "type": "System.DmlException",
                "message_sample": "UNABLE_TO_LOCK_ROW, unable to obtain exclusive access to this record",
                "first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-01T00:00:00Z",
                "occurrences": 1, "incident_ids": ["20260901T010000Z_c"], "resolution": None}
        seed_org("pub_org", "admin", "public", {"sig_npe": npe})
        seed_org("secret_org", "admin", "private", {"sig_lock": lock})

        h = rory.get("/api/home").json()
        check("home lists public orgs", "pub_org" in h["orgs"], str(h["orgs"].keys()))
        check("home hides private orgs you can't see", "secret_org" not in h["orgs"])
        check("incident counts come through", h["orgs"]["pub_org"]["incidents"] == 2, str(h["orgs"]["pub_org"]))
        check("last incident time is derived", h["orgs"]["pub_org"]["last_incident_at"] == "20260902T000000Z")
        check("recent fixes are listed", h["recent_fixes"] and h["recent_fixes"][0]["signature"] == "sig_npe")
        check("non-admins get no admin strip", "admin" not in h)
        ha = admin.get("/api/home").json()
        check("admins get the health strip", "admin" in ha and "llm" in ha["admin"], str(ha.get("admin")))
        check("admin sees the private org's unresolved count", ha["orgs"]["secret_org"]["unresolved"] == 1)

        print("\n-- known-issue matching --")
        pasted = ("System.NullPointerException: Attempt to de-reference a null object "
                  "Class.AgreementHelper.share: line 62, column 1 record a0B5g00000XyZ12EAB")
        m = rory.get("/api/triage/known", params={"q": pasted}).json()["matches"]
        check("a pasted exception finds the known issue", m and m[0]["signature"] == "sig_npe", str(m)[:300])
        check("...with its fix", m and bool(m[0]["resolution"]))
        m = rory.get("/api/triage/known", params={"q": "UNABLE_TO_LOCK_ROW unable to obtain exclusive access"}).json()["matches"]
        check("a private org's issue does not match for someone who can't see it", m == [], str(m))
        m = admin.get("/api/triage/known", params={"q": "UNABLE_TO_LOCK_ROW unable to obtain exclusive access"}).json()["matches"]
        check("...but does for an admin", m and m[0]["signature"] == "sig_lock", str(m)[:200])
        m = rory.get("/api/triage/known", params={"q": "totally unrelated words here"}).json()["matches"]
        check("unrelated text matches nothing", m == [], str(m)[:200])

        print("\n-- the assistant knows the app --")
        prompt = chat_agent.system_prompt("wendy", None)["content"]
        check("the app guide is in the system prompt", "ABOUT THIS APP" in prompt)

        print("\n-- deleting a user forgets their guide --")
        admin.delete(f"/api/admin/users/wendy")
        check("guide file removed", not os.path.exists(storage.guide_path("wendy")))

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("\nAll guide / home checks passed.")


if __name__ == "__main__":
    main()
