"""
"Which two Queueables are getting queued?" could not be answered (2026-10-08).

Log 20261008T183625Z_Debug_Log_07LUD00000C1jfK2AR: Apttus CreateOrderQJob ->
IbmcOrderTrigger -> IbmcOrderTriggerHandler.onBeforeUpdate line 254 throws
"Too many queueable jobs added to the queue: 2". Two gaps:

1. The normalizer kept no METHOD_ENTRY / CONSTRUCTOR_ENTRY / SYSTEM_METHOD_ENTRY
   events, so the log's own evidence of WHICH job classes were constructed
   and enqueued, and from where, was thrown away.
2. The org (00D3N0000008nBfv1) is extracted at 3.1.0 -- before
   async_dispatches (3.2.0) and call_sites (3.3.0). The card's silence was read
   as "no enqueue found" instead of "the org needs a Refresh".

Run:  python tests/test_log_async_jobs.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import kb_lookup, log_shape  # noqa: E402
from app.log_normalizer import parse_log_text  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {name}  {'' if condition else detail}")
    if not condition:
        FAILURES.append(name)


def make_log(system_level):
    t = iter(range(1, 10_000))

    def ln(rest):
        return f"18:36:25.{next(t)} ({next(t)})|{rest}"

    trig = ("CODE_UNIT_STARTED|[EXTERNAL]|01q000000000001|IbmcOrderTrigger on Apttus_Config2__Order__c "
            "trigger event BeforeUpdate|__sfdc_trigger/IbmcOrderTrigger")
    handler = ("IbmcOrderTriggerHandler.onBeforeUpdate(List<Apttus_Config2__Order__c>, "
               "Map<Id,Apttus_Config2__Order__c>)")
    out = [f"50.0 APEX_CODE,FINEST;APEX_PROFILING,NONE;DB,FINEST;SYSTEM,{system_level}",
           ln("EXECUTION_STARTED"),
           ln("CODE_UNIT_STARTED|[EXTERNAL]|01p000000000001|Apttus_CMConfig.CreateOrderQJob"),
           ln("ENTERING_MANAGED_PKG|Apttus_CMConfig"),
           # first trigger pass: enqueue #1 succeeds
           ln(trig),
           ln(f"METHOD_ENTRY|[15]|01p000000000002|{handler}"),
           ln("CONSTRUCTOR_ENTRY|[12]|01p000000000009|<init>()|IbmcOrderHelper"),
           ln("CONSTRUCTOR_EXIT|[12]|01p000000000009|<init>()|IbmcOrderHelper"),
           ln("CONSTRUCTOR_ENTRY|[240]|01p000000000003|<init>(Set<Id>)|itccBLPJobQueueable"),
           ln("CONSTRUCTOR_EXIT|[240]|01p000000000003|<init>(Set<Id>)|itccBLPJobQueueable")]
    if system_level == "FINE":
        out += [ln("SYSTEM_METHOD_ENTRY|[240]|System.enqueueJob(Object)"),
                ln("SYSTEM_METHOD_EXIT|[240]|System.enqueueJob(Object)")]
    out += [ln("USER_DEBUG|[241]|DEBUG|TCRM-2636 BLP JOB fired ..."),
            ln(f"METHOD_EXIT|[15]|01p000000000002|{handler}"),
            ln("CODE_UNIT_FINISHED|IbmcOrderTrigger on Apttus_Config2__Order__c trigger event BeforeUpdate"),
            # second pass (recursive Order update): enqueue #2 fails
            ln(trig),
            ln(f"METHOD_ENTRY|[15]|01p000000000002|{handler}"),
            ln("CONSTRUCTOR_ENTRY|[254]|01p000000000004|<init>(Id)|itccAgreementAutoActivationQueueable"),
            ln("CONSTRUCTOR_EXIT|[254]|01p000000000004|<init>(Id)|itccAgreementAutoActivationQueueable")]
    if system_level == "FINE":
        out += [ln("SYSTEM_METHOD_ENTRY|[254]|System.enqueueJob(Object)")]
    out += [ln("EXCEPTION_THROWN|[254]|System.LimitException: Too many queueable jobs added to the queue: 2"),
            ln("FATAL_ERROR|System.LimitException: Too many queueable jobs added to the queue: 2"),
            "",
            "Class.IbmcOrderTriggerHandler.onBeforeUpdate: line 254, column 1",
            "Trigger.IbmcOrderTrigger: line 15, column 1",
            ln("CODE_UNIT_FINISHED|IbmcOrderTrigger on Apttus_Config2__Order__c trigger event BeforeUpdate"),
            ln("CODE_UNIT_FINISHED|Apttus_CMConfig.CreateOrderQJob"),
            ln("EXECUTION_FINISHED")]
    return "\n".join(out)


def test_system_debug_log():
    n = parse_log_text(make_log("DEBUG"))
    a = n.get("async_jobs") or {}
    check("async_jobs section present", bool(a))
    check("entry unit recognised as async", (a.get("context") or {}).get("likely_async") is True)
    jobs = {(j["class"], j["line"], j["caller"]) for j in a.get("job_constructions", [])}
    check("first queueable constructed at 240 in onBeforeUpdate",
          ("itccBLPJobQueueable", 240, "IbmcOrderTriggerHandler.onBeforeUpdate") in jobs, str(jobs))
    check("second queueable constructed at 254 in onBeforeUpdate",
          ("itccAgreementAutoActivationQueueable", 254, "IbmcOrderTriggerHandler.onBeforeUpdate") in jobs)
    check("non-job helper constructor not listed", not any(c == "IbmcOrderHelper" for c, _, _ in jobs))
    f = (a.get("failed_async_limit") or [{}])[0]
    check("failing enqueue line + caller", f.get("line") == 254
          and f.get("caller") == "IbmcOrderTriggerHandler.onBeforeUpdate", str(f))
    check("failing enqueue names the job class",
          (f.get("job_class_guess") or {}).get("class") == "itccAgreementAutoActivationQueueable")
    check("attempted count parsed", f.get("attempted_count") == 2)
    check("visibility: SYSTEM below FINE explained",
          any("SYSTEM is below FINE" in v for v in a.get("visibility", [])))
    check("visibility: managed package hidden",
          any("ENTERING_MANAGED_PKG" in v for v in a.get("visibility", [])))
    check("no dispatches without SYSTEM=FINE", a.get("dispatches") == [])


def test_system_fine_log():
    n = parse_log_text(make_log("FINE"))
    d = n["async_jobs"]["dispatches"]
    check("both enqueueJob calls seen", len(d) == 2, str(d))
    check("first dispatch: line 240, BLP job, succeeded",
          d[0]["line"] == 240 and d[0]["job_class_guess"]["class"] == "itccBLPJobQueueable"
          and not d[0]["failed"])
    check("second dispatch: line 254, flagged failed",
          d[1]["line"] == 254 and d[1]["failed"]
          and d[1]["job_class_guess"]["class"] == "itccAgreementAutoActivationQueueable")


def test_shaper_keeps_async_jobs():
    n = parse_log_text(make_log("DEBUG"))
    n["user_debug"] = [f"noise {i} " + "z" * 200 for i in range(800)]
    r = {"meta": {"log_id": "L"}, "normalized_log": n}
    out = log_shape.shape_log_result(r, 30000)
    check("async_jobs survives shaping whole", out["normalized_log"].get("async_jobs") == n["async_jobs"])


def test_plain_sync_log_has_no_section():
    log = "\n".join(["59.0 APEX_CODE,FINE;SYSTEM,FINE",
                     "1|CODE_UNIT_STARTED|[EXTERNAL]|execute_anonymous_apex",
                     "2|CODE_UNIT_FINISHED|execute_anonymous_apex"])
    check("nothing async -> no async_jobs key", "async_jobs" not in parse_log_text(log))


def test_stale_card_note():
    card = {"id": "IbmcOrderTriggerHandler", "type": "ApexClass", "extractor_version": "3.1.0",
            "calls_to": []}
    s = kb_lookup.stale_note(card)
    check("3.1.0 card is stale", s is not None)
    check("names async_dispatches and call_sites",
          s and [m["section"] for m in s["missing_features"]] == ["async_dispatches", "call_sites"])
    check("says Refresh", s and "Refresh" in s["meaning"])
    fresh = dict(card, extractor_version="3.3.0", async_dispatches=[], call_sites=[])
    check("3.3.0 card is not stale", kb_lookup.stale_note(fresh) is None)
    check("flows are never flagged", kb_lookup.stale_note({"type": "Flow", "extractor_version": "1"}) is None)
    shaped = kb_lookup.shape(dict(card, _stale=s, soql=[{"x": "y" * 500}] * 200), 6000)
    check("_stale survives card shaping", "_stale" in shaped)
    org = kb_lookup.org_stale_note({"A": card, "B": dict(card, extractor_version="3.3.0")})
    check("org-level note uses the oldest card", org and org["extractor_version"] == "3.1.0")


if __name__ == "__main__":
    for t in (test_system_debug_log, test_system_fine_log, test_shaper_keeps_async_jobs,
              test_plain_sync_log_has_no_section, test_stale_card_note):
        print(t.__name__)
        t()
    print("FAILED: " + ", ".join(FAILURES) if FAILURES else "all passed")
    sys.exit(1 if FAILURES else 0)
