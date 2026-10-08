"""
Large normalized logs reached the chat model as {"omitted": true} (2026-10-07).

chat._truncate ran every over-budget dict through kb_lookup.shape(), which
trims list sections but omits any other value whole. A log result is
{"meta", "normalized_log": {...}} -- so for any log over 24 KB the entire
normalized payload was dropped and gpt-6.1-sol (correctly) refused to do an
RCA. gpt-4o "worked" only because it was tried on an 8 KB log.

Run:  python -m pytest tests/test_log_shape.py -q
 or:  python tests/test_log_shape.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import chat, log_shape  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


FIELDS = ",".join(f"Apttus_Config2__Field{i}__c" for i in range(160))


def big_log():
    """Shaped like the real 197 KB EQ_Test1 log: a huge execution tree, fat
    SELECT lists, one unit that threw deep in the middle."""
    units = []
    for i in range(1700):
        units.append({"label": f"__sfdc_trigger/Apttus_Config2/T{(i // 4) % 7}", "depth": 1 + (i // 4) % 3,
                      "had_exception": False})
    units[900] = {"label": "APTS_LineItemTrigger", "depth": 2, "had_exception": True}
    soql = [{"signature": f"SELECT Id,{FIELDS},CPQ_Annual_List_Price__c FROM Apttus_Config2__LineItem__c WHERE Id = ?",
             "object": "Apttus_Config2__LineItem__c", "rows": 1,
             "raw_example": "SELECT Id," + FIELDS[:140] + "...", "occurrences": 3, "total_rows": 3},
            {"signature": f"SELECT Id,{FIELDS},(SELECT Id FROM Children__r) FROM Apttus_Config2__LineItem__c WHERE ConfigurationId = ?",
             "object": "Apttus_Config2__LineItem__c", "rows": 400,
             "raw_example": "x", "occurrences": 40, "total_rows": 16000}]
    soql += [{"signature": f"SELECT Id,{FIELDS} FROM Obj{i}__c WHERE Id = ?", "object": f"Obj{i}__c",
              "rows": 1, "raw_example": "x", "occurrences": 1, "total_rows": i} for i in range(25)]
    return {
        "header": {"api_version": "59.0"},
        "execution_units": units,
        "exceptions": [{"type": "System.SObjectException",
                        "message": "System.SObjectException: SObject row was retrieved via SOQL without "
                                   "querying the requested field: Apttus_Config2__LineItem__c.CPQ_AnnualNetPrice__c",
                        "stack": ["Class.APTS_LineItemHelper.calc: line 42"], "occurrences": 3}],
        "soql_summary": soql,
        "dml_summary": [{"operation": "Update", "object": "X", "occurrences": 7, "total_rows": 1548}],
        "callouts": [],
        "user_debug": [f"debug line {i} " + "y" * 60 for i in range(400)],
        "validation_failures": [],
        "flow_events": [],
        "limits_final": {"SOQL queries": {"used": 101, "max": 100}},
        "limits_by_namespace": {"(default)": {"SOQL queries": {"used": 101, "max": 100}}},
        "involved_components": ["APTS_LineItemTrigger"],
    }


def wrap(n):
    return {"meta": {"log_id": "20261007T000000Z_big", "involved_components": n["involved_components"]},
            "normalized_log": n}


def test_regression_old_shaper_omitted_everything():
    # Documents the bug: the card shaper cannot descend into normalized_log.
    from app import kb_lookup
    shaped = kb_lookup.shape(wrap(big_log()), 24000)
    check("old card shaper omitted normalized_log (the bug)",
          shaped.get("normalized_log") is None
          and shaped["_truncated"]["sections"].get("normalized_log", {}).get("omitted"))


def test_chat_truncate_keeps_evidence():
    result = wrap(big_log())
    raw = len(json.dumps(result))
    text, truncated = chat._truncate(result)
    out = json.loads(text)
    n = out.get("normalized_log") or {}
    check("raw log is far over the budget", raw > chat.MAX_LOG_RESULT_BYTES * 2, str(raw))
    check("flagged truncated", truncated is True)
    check("fits the log budget", len(text) <= chat.MAX_LOG_RESULT_BYTES, str(len(text)))
    check("normalized_log present, not omitted", isinstance(n, dict) and "omitted" not in n)
    check("exceptions kept whole", n.get("exceptions") == result["normalized_log"]["exceptions"])
    check("limits kept whole", n.get("limits_final") and n.get("limits_by_namespace"))
    units = n.get("execution_units") or []
    check("unit that threw is kept", any(u.get("had_exception") for u in units),
          str(len(units)))
    check("every list section got some room",
          all(n.get(k) for k in ("execution_units", "soql_summary", "user_debug", "dml_summary")))
    soql = n.get("soql_summary") or []
    check("heaviest query kept", any(q.get("total_rows") == 16000 for q in soql))
    check("SELECT lists collapsed", all("<" in q["signature"] for q in soql))
    check("missing-field evidence preserved in the collapsed SELECT",
          any("DOES NOT include CPQ_AnnualNetPrice__c" in q["signature"] for q in soql),
          soql[0]["signature"][:200] if soql else "")
    check("subquery FROM did not fool the FROM scan",
          any("FROM Apttus_Config2__LineItem__c WHERE ConfigurationId" in q["signature"]
              and "1 subquery" in q["signature"] for q in soql))
    note = out.get("_truncated") or {}
    check("note names trimmed sections", "execution_units" in note.get("sections", {}))
    check("note points at get_normalized_log paging, not get_component",
          "get_normalized_log(log_id='20261007T000000Z_big'" in note.get("how_to_get_the_rest", "")
          and "get_component" not in note.get("how_to_get_the_rest", ""))
    check("execution units run-length collapsed", any(u.get("repeat") for u in units)
          and any("run-length" in c for c in note.get("compacted", [])))


def test_small_log_untouched():
    n = big_log()
    n["execution_units"] = n["execution_units"][:5]
    n["soql_summary"] = n["soql_summary"][:1]
    n["user_debug"] = n["user_debug"][:5]
    text, truncated = chat._truncate(wrap(n))
    check("small log passes through verbatim", not truncated and json.loads(text)["normalized_log"] == n)


def test_unstored_log_hint():
    r = {"normalized_log": big_log(), "stored": False, "log_id": None, "meta": None}
    out = log_shape.shape_log_result(r, 30000)
    check("unstored log fits", len(json.dumps(out)) <= 30000, str(len(json.dumps(out))))
    check("unstored hint says store=true", "store=true" in out["_truncated"]["how_to_get_the_rest"])


def test_get_normalized_log_paging():
    import mcp_server
    full = wrap(big_log())

    async def fake_get(path, params=None):
        return json.loads(json.dumps(full))

    orig = mcp_server._get
    mcp_server._get = fake_get
    try:
        r = asyncio.run(mcp_server.get_normalized_log("20261007T000000Z_big",
                                                      sections=["soql_summary", "nope"],
                                                      offset=1, limit=2))
    finally:
        mcp_server._get = orig
    n = r["normalized_log"]
    check("only the asked section", list(n) == ["soql_summary"])
    check("offset/limit applied", n["soql_summary"] == full["normalized_log"]["soql_summary"][1:3])
    check("page info", r["_page"]["soql_summary"] == {"total": 27, "offset": 1, "returned": 2,
                                                       "next_offset": 3}, str(r["_page"]))
    check("unknown section reported", r.get("_missing_sections") == ["nope"])
    text, truncated = chat._truncate(r)
    check("a 2-query page fits uncompacted (full field list visible)",
          not truncated and FIELDS in text)


if __name__ == "__main__":
    for t in (test_regression_old_shaper_omitted_everything, test_chat_truncate_keeps_evidence,
              test_small_log_untouched, test_unstored_log_hint, test_get_normalized_log_paging):
        print(t.__name__)
        t()
    print("FAILED: " + ", ".join(FAILURES) if FAILURES else "all passed")
    sys.exit(1 if FAILURES else 0)
