"""
Outbound API questions on large Apex classes (2026-10-05, org 00DDL00000CU2dS).

The question "which Apttus APIs does APTS_CustomSolutionctrl call, in what
order, under what conditions" failed three ways at once:

  1. get_component returned a 52 KB card; chat cut it at 24 KB mid-`soql`, and
     `calls_to` -- the section with the answer -- came after the cut.
  2. get_inbound_references('Apttus_CPQApi.CPQWebService') found nothing: the
     index key is the bare 'CPQWebService' (and a second caller wrote
     'CPQWebservice' -- Apex is case-insensitive).
  3. Even untruncated, calls_to is aggregated per class: no method, line or
     branch, so ORDER and CONDITIONS were unanswerable.

Run:  python -m pytest tests/test_call_sites.py -q
 or:  python tests/test_call_sites.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app.extractors import apex  # noqa: E402
from app import index_builder, kb_lookup  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


SRC = '''
public without sharing class APTS_Demo {
    public static Boolean flag = false;
    @RemoteAction
    public static Boolean addConstraintRules(Id cartId) {
        // Apttus_CPQApi.CPQWebService.inAComment(   <- must not count
        Apttus_CPQApi.CPQWebService.associateConstraintRules(cartId, null);
        Apttus_CPQApi.CPQWebservice.applyConstraintRules(cartId, true);
        return true;
    }
    @RemoteAction
    public static List<Boolean> addToCart(List<String> ids) {
        Apttus_CPQApi.CPQ.AddMultiProductRequestDO req = new Apttus_CPQApi.CPQ.AddMultiProductRequestDO();
        if (ids != null && !ids.isEmpty()) {
            for (String s : ids) {
                APTS_FL_Utility.returnCustomData(s);
            }
            Apttus_CPQApi.CPQ.AddMultiProductResponseDO res = Apttus_CPQApi.CPQWebService.addMultiProducts(req);
            if (res != null) addConstraintRules(req.CartId);
            else {
                helper();
            }
        } else if (flag) {
            this.helper();
        }
        try { Apttus_CPQApi.CPQWebService.addBundle(null); } catch (Exception e) { System.debug(e); }
        Account.SObjectType.getDescribe();
        return new List<Boolean>();
    }
    private static void helper() { String x = 'a'; x.toLowerCase(); }
}
'''
CLASSES = {"APTS_Demo", "CPQWebService", "CPQ", "APTS_FL_Utility"}


def _card():
    return apex.parse_class("APTS_Demo", SRC, {"Account"}, CLASSES)


def test_extractor():
    print("\nextractor: call_sites and namespaces")
    card = _card()
    sites = card["call_sites"]
    callees = [s["callee"] for s in sites]
    check("a comment is not a call", not any("inAComment" in c for c in callees))
    check("sites are in source order", [s["line"] for s in sites] == sorted(s["line"] for s in sites))
    by = {s["method_called"]: s for s in sites}

    a = by["associateConstraintRules"]
    check("the namespace-qualified callee is kept as written",
          a["callee"] == "Apttus_CPQApi.CPQWebService.associateConstraintRules", a["callee"])
    check("...attributed to its calling method", a["caller_method"] == "addConstraintRules")
    check("...with the namespace", a["namespace"] == "Apttus_CPQApi")
    check("a differently-cased receiver maps to the declared class",
          by["applyConstraintRules"]["target"] == "CPQWebService", by["applyConstraintRules"]["target"])

    mp = by["addMultiProducts"]
    check("a guarded call carries its condition",
          mp.get("conditions") == ["if (ids != null && !ids.isEmpty())"], str(mp.get("conditions")))
    loop = by["returnCustomData"]
    check("a call in a loop says so", loop["in_loop"] and loop["conditions"][-1].startswith("for ("))

    local = [s for s in sites if s["kind"] == "same_class"]
    check("a call to a local helper is recorded as same_class",
          any(s["method_called"] == "addConstraintRules" and s["caller_method"] == "addToCart"
              for s in local))
    else_site = next(s for s in local if s["method_called"] == "helper" and s["line"] < 24)
    check("an else branch names what it excludes",
          else_site["conditions"][-1] == "else [not: res != null]", str(else_site["conditions"]))
    elif_site = next(s for s in local if s["callee"] == "this.helper")
    check("an else-if names the branch it follows",
          "else if (flag)" in elif_site["conditions"][0]
          and "ids != null" in elif_site["conditions"][0], str(elif_site["conditions"]))
    check("a call inside try says so", by["addBundle"].get("conditions") == ["try"],
          str(by["addBundle"].get("conditions")))
    check("new Ns.X() is a constructor, not an API call",
          by["AddMultiProductRequestDO"]["kind"] == "constructor")
    check("the method declaration itself is not a call",
          not any(s["method_called"] == "addConstraintRules" and s["line"] == 5 for s in sites))
    check("system and object receivers are not sites",
          not any(s["method_called"] in ("debug", "getDescribe", "toLowerCase") for s in sites))

    calls = {c["target"]: c for c in card["calls_to"]}
    check("calls_to keeps the namespace", calls["CPQWebService"].get("namespace") == "Apttus_CPQApi")
    check("calls_to merges the case variant",
          "CPQWebservice" not in calls and "applyConstraintRules" in calls["CPQWebService"]["methods_called"])


def test_index():
    print("\nindex: managed targets and inbound call sites")
    caller = _card()
    stub = {"id": "CPQWebService", "type": "ApexClass", "namespace": "Apttus_CPQApi",
            "is_managed": True, "is_customer_authored": False, "calls_to": [], "call_sites": []}
    built = index_builder.build_index({"APTS_Demo": caller, "CPQWebService": stub}, {}, {})
    ct = {c["target"]: c for c in built["org_index"]["APTS_Demo"]["calls_to"]}
    check("a call into a managed stub is marked managed_package_class",
          ct["CPQWebService"]["kind"] == "managed_package_class", ct["CPQWebService"]["kind"])
    rows = built["inbound_index"]["CPQWebService"]["called_by"]
    row = next(r for r in rows if r["id"] == "APTS_Demo")
    meths = [s["method_called"] for s in row["call_sites"]]
    check("the inbound row lists each call site",
          {"associateConstraintRules", "applyConstraintRules", "addMultiProducts", "addBundle"}
          <= set(meths), str(meths))
    site = next(s for s in row["call_sites"] if s["method_called"] == "addMultiProducts")
    check("...with caller method, line and conditions",
          site["caller_method"] == "addToCart" and site["line"] and site.get("conditions"))
    return built


def test_lookup(built):
    print("\nlookup: resolving names the way people write them")
    idx, inb = built["org_index"], built["inbound_index"]
    cid, m, note = kb_lookup.resolve(inb, "Apttus_CPQApi.CPQWebService", cards=idx)
    check("Ns.Class resolves to the stub id", cid == "CPQWebService" and m is None, str(cid))
    cid, m, _ = kb_lookup.resolve(inb, "Apttus_CPQApi.CPQWebService.associateConstraintRules",
                                  cards=idx)
    check("Ns.Class.method resolves with a method filter",
          cid == "CPQWebService" and m == "associateConstraintRules")
    cid, _, _ = kb_lookup.resolve(inb, "Wrong_NS.CPQWebService", cards=idx)
    check("a wrong namespace does not resolve", cid is None, str(cid))
    cid, m, _ = kb_lookup.resolve(idx, "apts_demo.addToCart")
    check("Class.method, any case", cid == "APTS_Demo" and m == "addToCart")
    check("unknown stays unknown", kb_lookup.resolve(idx, "Nope.Nothing")[0] is None)
    check("case variants are found",
          kb_lookup.case_variants({"CPQWebService": 1, "CPQWebservice": 2, "X": 3},
                                  "cpqwebservice") == ["CPQWebService", "CPQWebservice"])


def test_shape():
    print("\nshape: trimming without losing the answer")
    card = _card()
    card["soql"] = [{"object": "Apttus_Config2__ProductConfiguration__c", "fields": ["F%d__c" % i
                     for i in range(40)], "method": "initConfig", "line": i} for i in range(60)]
    card["field_writes"] = [{"field": "X__c", "method": "m", "rhs": "y" * 300} for _ in range(60)]
    big = len(json.dumps(card))
    s = kb_lookup.shape(card, 12000)
    out = len(json.dumps(s))
    check(f"a {big}-char card fits the budget", out <= 12000, str(out))
    check("calls_to and call_sites survive whole",
          s["calls_to"] == card["calls_to"] and s["call_sites"] == card["call_sites"])
    check("the bulky sections are the ones trimmed",
          set(s["_truncated"]["sections"]) <= {"soql", "field_writes", "methods"},
          str(s["_truncated"]["sections"]))
    check("and the note says how to get the rest",
          "sections=" in s["_truncated"]["how_to_get_the_rest"])
    check("a small card is untouched", kb_lookup.shape({"id": "x"}, 12000) == {"id": "x"})

    f = kb_lookup.filter_method(card, "addConstraintRules")
    check("method filter keeps that method's call sites",
          {x["method_called"] for x in f["call_sites"]}
          == {"associateConstraintRules", "applyConstraintRules"}, str(f["call_sites"]))
    check("...and, separately, where the class calls it",
          [x["caller_method"] for x in f.get("_called_from_in_class", [])] == ["addToCart"])
    check("...and its methods entry", [x["name"] for x in f["methods"]] == ["addConstraintRules"])
    sel = kb_lookup.select_sections(card, ["calls_to", "nope"])
    check("sections keeps identity + requested",
          "calls_to" in sel and "soql" not in sel and sel["id"] == "APTS_Demo"
          and sel["_missing_sections"] == ["nope"])


def test_routes(built):
    print("\nroutes: REST get_component / inbound on a stub KB")
    from fastapi.testclient import TestClient
    from app import main, storage

    kb = {"org_index": built["org_index"], "inbound_index": built["inbound_index"],
          "object_touch_map": {}, "field_touch_map": {}, "entry_points_index": {},
          "org_stats": {}, "file_hashes": {"classes/APTS_Demo.cls": {
              "hash": "h", "first_seen": "2026-10-01T22:05:25Z", "last_changed": "2026-10-05T16:33:51Z"}}}
    real = storage.load_kb
    storage.load_kb = lambda org_id: kb
    try:
        # Exercise the route functions directly; auth is covered elsewhere.
        r = main.get_inbound("org", "Apttus_CPQApi.CPQWebService.associateConstraintRules")
        check("inbound: qualified name with method finds the caller",
              [x["id"] for x in r["called_by"]] == ["APTS_Demo"], str(r)[:300])
        check("inbound: call sites narrowed to that method",
              {s["method_called"] for s in r["called_by"][0]["call_sites"]}
              == {"associateConstraintRules"})
        check("inbound: says how it resolved", "Apttus_CPQApi" in r.get("_resolved", ""))
        check("inbound: reports the target's namespace", r.get("target_namespace") == "Apttus_CPQApi")

        c = main.get_component("org", "APTS_Demo", sections="call_sites", method=None, max_chars=None)
        check("component: sections filter", "call_sites" in c and "soql" not in c)
        c = main.get_component("org", "apts_demo.addToCart", sections=None, method=None,
                               max_chars=None)
        check("component: Class.method implies a method filter",
              c.get("_filter", {}).get("method") == "addToCart")
        full = main.get_component("org", "APTS_Demo", sections=None, method=None, max_chars=None)
        stored = built["org_index"]["APTS_Demo"]
        check("component: no params = the full card plus change dates (UI contract)",
              {k: v for k, v in full.items()
               if k not in ("source_first_seen", "source_last_changed")} == stored)
        check("component: says when the source last changed",
              full.get("source_last_changed") == "2026-10-05T16:33:51Z")
        check("component: the stored card is not mutated", "source_last_changed" not in stored)
        c = main.get_component("org", "APTS_Demo", sections="calls_to", method=None, max_chars=None)
        check("component: change dates survive a sections filter", "source_last_changed" in c)
    finally:
        storage.load_kb = real


def test_chat_truncate():
    print("\nchat: oversized dict results are shaped, not byte-cut")
    from app import chat
    card = _card()
    card["soql"] = [{"object": "O__c", "fields": ["F%d__c" % i for i in range(60)]}
                    for _ in range(200)]
    text, truncated = chat._truncate(card)
    check("result is still valid JSON", isinstance(json.loads(text), dict))
    check("flagged as truncated", truncated is True)
    check("calls_to survived", "calls_to" in json.loads(text))


def test_real_card():
    """The actual card from the failing chat, if this checkout has it."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "orgs",
                        "00DDL00000CU2dS", "knowledge_base", "org_index.json")
    if not os.path.exists(path):
        print("\nreal card: skipped (org data not present)")
        return
    print("\nreal card: APTS_CustomSolutionctrl from 00DDL00000CU2dS")
    with open(path, encoding="utf-8") as f:
        card = json.load(f)["APTS_CustomSolutionctrl"]
    s = kb_lookup.shape(card, 20000)
    check("the 52 KB card fits in 20 KB", len(json.dumps(s)) <= 20000)
    cpq = next((c for c in s.get("calls_to", []) if c["target"] == "CPQWebService"), None)
    check("and the CPQWebService calls are visible",
          cpq and "associateConstraintRules" in cpq["methods_called"], str(cpq))


def main_():
    test_extractor()
    built = test_index()
    test_lookup(built)
    test_shape()
    test_routes(built)
    test_chat_truncate()
    test_real_card()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All call-site checks passed.")
    return 0


def test_all():
    assert main_() == 0


if __name__ == "__main__":
    sys.exit(main_())
