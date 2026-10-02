"""
Filing an incident (POST /api/orgs/{org}/incidents): from an uploaded log,
from a log already in the normalized-log library, or from a suspect field --
plus the hardening around it (label slugging, id collisions, warnings,
back-links from the library log to the incident).

Run:  python tests/test_incident_filing.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-incident-test-")
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
from app.log_normalizer import parse_log_text, involved_components  # noqa: E402

FAILURES = []

LOG = """64.0 APEX_CODE,FINEST;DB,INFO
12:00:00.0 (1)|EXECUTION_STARTED
12:00:00.0 (2)|CODE_UNIT_STARTED|[EXTERNAL]|01q000000000001|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (3)|EXCEPTION_THROWN|[12]|System.NullPointerException: Attempt to de-reference a null object
12:00:00.0 (4)|FATAL_ERROR|System.NullPointerException: Attempt to de-reference a null object

Class.QuoteHelper.recalc: line 40, column 1
Trigger.QuoteTrigger: line 12, column 1
12:00:00.0 (5)|CODE_UNIT_FINISHED|QuoteTrigger on Quote trigger event BeforeUpdate
12:00:00.0 (6)|EXECUTION_FINISHED
"""

ORG_INDEX = {
    "QuoteTrigger": {"id": "QuoteTrigger", "type": "ApexTrigger", "object": "Quote", "file": "QuoteTrigger.trigger"},
    "QuoteHelper": {"id": "QuoteHelper", "type": "ApexClass", "file": "QuoteHelper.cls"},
    "Unrelated": {"id": "Unrelated", "type": "ApexClass", "file": "Unrelated.cls"},
}


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
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_index.json"), ORG_INDEX)
    storage.write_json(os.path.join(storage.kb_dir(org_id), "org_stats.json"), {"counts": {}})
    storage.write_json(os.path.join(storage.kb_dir(org_id), "field_touch_map.json"),
                       {"Discount__c": {"writers": [{"component": "QuoteHelper", "risk": "high", "example": "0"}]}})


def login(u, p):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return c


def store_log(client, label, org_id=None):
    data = {"store": "true", "label": label}
    if org_id:
        data["org_id"] = org_id
    r = client.post("/api/logs/normalize", data=data, files={"log_file": ("quote.log", LOG.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()["log_id"]


def main():
    with TestClient(app) as admin:
        admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        for n, role in (("alice", "user"), ("bob", "user"), ("rita", "reader")):
            r = admin.post("/api/admin/users", json={"username": n, "password": "password123", "role": role})
            assert r.status_code in (200, 201), r.text
        seed("acmeprod", "alice", "public", account="Acme Corp")
        seed("acmeuat", "alice", "private")
        alice, bob, rita = login("alice", "password123"), login("bob", "password123"), login("rita", "password123")

        print("\n-- involved_components re-match --")
        n0 = parse_log_text(LOG)
        n1 = parse_log_text(LOG, index_ids=set(ORG_INDEX))
        check("library parse finds no org components (prefix heuristic)", n0["involved_components"] == [])
        check("re-match equals parsing against the org",
              involved_components(n0["execution_units"], n0["exceptions"], set(ORG_INDEX)) == n1["involved_components"],
              n1["involved_components"])

        print("\n-- file from an uploaded log (baseline) --")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"label": "first npe"},
                       files={"log_file": ("quote.log", LOG.encode(), "text/plain")})
        check("upload filing works", r.status_code == 200, r.text)
        m_up = r.json()["meta"]
        check("label is slugged (spaces ok)", m_up["incident_id"].endswith("_first_npe"), m_up["incident_id"])
        check("free-text label kept for display", m_up["label"] == "first npe")
        check("new issue", m_up["recurrence"] is False and m_up["signature"])
        check("filed_by recorded", m_up["filed_by"] == "alice")
        check("no library copy unless asked", m_up["source_log_id"] is None)

        print("\n-- file from a library log --")
        lib_id = store_log(alice, "quote npe in lib", org_id="acmeprod")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"log_id": lib_id})
        check("library filing works", r.status_code == 200, r.text)
        body = r.json()
        m_lib = body["meta"]
        check("matches the same signature -> recurrence", m_lib["recurrence"] is True and m_lib["signature"] == m_up["signature"])
        check("source_log_id recorded", m_lib["source_log_id"] == lib_id)
        check("source_log is the stored file name", m_lib["source_log"] == "quote.log", m_lib["source_log"])
        check("no warnings for a same-org first filing", body["warnings"] == [], body["warnings"])
        full = alice.get(f"/api/orgs/acmeprod/incidents/{m_lib['incident_id']}").json()
        check("components re-matched against the org",
              set(full["normalized_log"]["involved_components"]) == {"QuoteTrigger", "QuoteHelper"},
              full["normalized_log"]["involved_components"])
        check("context pack has the org's components",
              {"QuoteTrigger", "QuoteHelper"} <= set(full["rca_context_pack"]["primary_components"]))
        check("stored library log itself is unchanged",
              storage.load_normalized_log(lib_id)["normalized_log"]["involved_components"] == [])

        lm = alice.get(f"/api/logs/{lib_id}").json()["meta"]
        check("library log back-links the incident",
              [l["incident_id"] for l in lm.get("incidents", [])] == [m_lib["incident_id"]], lm.get("incidents"))

        r = alice.post("/api/orgs/acmeprod/incidents", data={"log_id": lib_id})
        check("re-filing the same log warns", any("already filed" in w for w in r.json()["warnings"]), r.json()["warnings"])

        print("\n-- cross-org + visibility --")
        r = alice.post("/api/orgs/acmeuat/incidents", data={"log_id": lib_id})
        check("log tagged to another org warns", r.status_code == 200
              and any("tagged to org 'acmeprod'" in w for w in r.json()["warnings"]), r.json())
        check("private-org back-link hidden from others",
              all(l["org_id"] != "acmeuat" for l in bob.get(f"/api/logs/{lib_id}").json()["meta"]["incidents"]))
        check("...but shown to the org owner",
              any(l["org_id"] == "acmeuat" for l in alice.get(f"/api/logs/{lib_id}").json()["meta"]["incidents"]))

        private_log = store_log(alice, "uat only", org_id="acmeuat")
        r = bob.post("/api/orgs/acmeprod/incidents", data={"log_id": private_log})
        check("cannot file from a log you cannot see (404)", r.status_code == 404, r.status_code)
        r = rita.post("/api/orgs/acmeprod/incidents", data={"log_id": lib_id})
        check("reader cannot file (403)", r.status_code == 403, r.status_code)
        r = alice.post("/api/orgs/acmeprod/incidents", data={"log_id": "../../etc"})
        check("bad log id is 404", r.status_code == 404, r.status_code)

        print("\n-- input validation --")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"log_id": lib_id},
                       files={"log_file": ("quote.log", LOG.encode(), "text/plain")})
        check("file + log_id rejected (400)", r.status_code == 400, r.status_code)
        r = alice.post("/api/orgs/acmeprod/incidents", data={"label": "x", "field": "  "})
        check("nothing to file rejected (400)", r.status_code == 400, r.status_code)

        print("\n-- label / id hardening --")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"label": "../../escape", "field": "Discount__c"})
        iid = r.json()["meta"]["incident_id"]
        check("path characters slugged out", r.status_code == 200 and "/" not in iid and ".." not in iid, iid)
        check("nothing written outside the incidents folder",
              not os.path.exists(os.path.join(storage.ORGS_ROOT, "escape")))
        ids = set()
        for _ in range(3):
            ids.add(alice.post("/api/orgs/acmeprod/incidents", data={"label": "dup", "field": "Discount__c"})
                    .json()["meta"]["incident_id"])
        check("same label in the same second never overwrites", len(ids) == 3, ids)
        check("dot-dot incident id is 404", alice.get("/api/orgs/acmeprod/incidents/..").status_code == 404)

        print("\n-- field warnings --")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"field": "Nope__c"})
        check("field with no writers warns", any("No automation" in w for w in r.json()["warnings"]), r.json()["warnings"])
        r = alice.post("/api/orgs/acmeprod/incidents", data={"field": "Discount__c"})
        check("field with writers does not", r.json()["warnings"] == [], r.json()["warnings"])

        print("\n-- upload + save to library --")
        r = alice.post("/api/orgs/acmeprod/incidents", data={"label": "keep me", "save_log": "true"},
                       files={"log_file": ("keep.log", LOG.encode(), "text/plain")})
        m_keep = r.json()["meta"]
        check("save_log stores a library copy", r.status_code == 200 and m_keep["source_log_id"], r.text)
        stored = alice.get(f"/api/logs/{m_keep['source_log_id']}").json()
        check("library copy tagged to the org + its account",
              stored["meta"]["org_id"] == "acmeprod" and stored["meta"]["account"] == "Acme Corp")
        check("library copy owned by the filer", stored["meta"]["owner"] == "alice")
        check("library copy back-links", stored["meta"]["incidents"][0]["incident_id"] == m_keep["incident_id"])
        check("raw header line not stored", "APEX_CODE,FINEST" not in str(stored["normalized_log"].get("execution_units")))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("All incident filing checks passed.")


if __name__ == "__main__":
    main()
