"""
build_index.py — merges apex/flow/lwc component cards into a single
org-wide customization knowledge base:

  org_index.json        - every component keyed by id, one place to look up
  object_touch_map.json - ObjectApiName -> which classes/triggers/flows read
                          or write it (the key lookup for "what could have
                          touched Quote__c during this transaction?")
  call_graph.json        - class -> classes it calls, and the reverse
                          (who calls this class) — for tracing blame past
                          the top frame in a stack trace
  field_touch_map.json  - FieldApiName__c -> every component that writes it,
                          ranked by risk (map_lookup off a never-cleared
                          static collection = high). This is what answers
                          "field X had the wrong value, no exception was
                          thrown, where do I even start" using nothing but
                          the knowledgebase -- no raw source access needed.
  org_stats.json         - org-wide rollup used for scoping/estimating an
                          engagement and for spotting risk concentrations
                          (untested batch classes, classes with callouts,
                          never-cleared static collections, etc.)

Usage:
    python build_index.py <cards_dir> <out_dir>
"""
import os
import sys
import json
from collections import defaultdict

from common import write_json


def main():
    cards_dir, out_dir = sys.argv[1:3]

    apex = json.load(open(os.path.join(cards_dir, "apex_cards.json")))
    flows = json.load(open(os.path.join(cards_dir, "flow_cards.json")))
    lwc = json.load(open(os.path.join(cards_dir, "lwc_cards.json")))

    org_index = {}
    org_index.update(apex)
    org_index.update(flows)
    org_index.update(lwc)

    object_touch = defaultdict(lambda: {"read": set(), "write": set(), "trigger": set(), "flow": set()})

    for cid, card in apex.items():
        if card["type"] not in ("ApexClass", "ApexTrigger"):
            continue
        for obj in card.get("objects_referenced", []):
            object_touch[obj]["read"].add(cid)
        for d in card.get("dml", []):
            tgt = d.get("target")
            # target is a variable name, not necessarily the object name;
            # objects_referenced is the reliable signal, dml just proves a
            # write of *some* kind happens in this component.
            for obj in card.get("objects_referenced", []):
                object_touch[obj]["write"].add(cid)
        if card["type"] == "ApexTrigger" and card.get("object"):
            object_touch[card["object"]]["trigger"].add(cid)

    for cid, card in flows.items():
        if card.get("start_object"):
            object_touch[card["start_object"]]["flow"].add(cid)

    object_touch_out = {
        obj: {k: sorted(v) for k, v in touches.items() if v}
        for obj, touches in sorted(object_touch.items())
    }

    call_graph = {}
    reverse_call_graph = defaultdict(set)
    for cid, card in apex.items():
        calls = card.get("calls_to", [])
        call_graph[cid] = calls
        for callee in calls:
            reverse_call_graph[callee].add(cid)
    reverse_call_graph_out = {k: sorted(v) for k, v in reverse_call_graph.items()}

    risk_rank = {"high": 0, "medium": 1, "low": 2}
    field_touch = defaultdict(list)
    never_cleared_statics = []
    for cid, card in apex.items():
        for s in card.get("static_mutable_state", []):
            if not s.get("cleared_or_reassigned_elsewhere"):
                never_cleared_statics.append({
                    "component": cid, "field": s["name"], "type": s["collection_type"],
                })
        for w in card.get("field_writes", []):
            field_touch[w["field"]].append({
                "component": cid,
                "risk": w["risk"],
                "pattern": w["pattern"],
                "source_map": w.get("source_map"),
                "reason": w.get("reason"),
                "example": w.get("rhs"),
            })
    field_touch_out = {}
    for field, writers in field_touch.items():
        # de-dupe repeated identical (component, risk, source_map) entries --
        # a field can legitimately be written from several distinct lines in
        # the same class, but that's noise for this lookup, not signal.
        seen = set()
        deduped = []
        for w in sorted(writers, key=lambda w: risk_rank.get(w["risk"], 9)):
            key = (w["component"], w["risk"], w["source_map"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(w)
        field_touch_out[field] = deduped

    stats = {
        "counts": {
            "apex_classes": sum(1 for c in apex.values() if c["type"] == "ApexClass"),
            "apex_triggers": sum(1 for c in apex.values() if c["type"] == "ApexTrigger"),
            "test_classes": sum(1 for c in apex.values() if c.get("is_test_class")),
            "flows": len(flows),
            "lwc_components": len(lwc),
        },
        "async_job_classes": {
            "batchable": [c["id"] for c in apex.values() if any(e["kind"] == "Batchable" for e in c.get("entry_points", []))],
            "queueable": [c["id"] for c in apex.values() if any(e["kind"] == "Queueable" for e in c.get("entry_points", []))],
            "schedulable": [c["id"] for c in apex.values() if any(e["kind"] == "Schedulable" for e in c.get("entry_points", []))],
            "future": [c["id"] for c in apex.values() if any(e["kind"] == "future" for e in c.get("entry_points", []))],
        },
        "integration_points": {
            "classes_with_callouts": [c["id"] for c in apex.values() if c.get("callouts")],
            "named_credentials_referenced": sorted({nc for c in apex.values() for nc in c.get("named_credentials", [])}),
        },
        "custom_exceptions_defined": sorted({e for c in apex.values() for e in c.get("custom_exceptions_defined", [])}),
        "most_referenced_objects": sorted(
            object_touch_out.items(),
            key=lambda kv: sum(len(v) for v in kv[1].values()),
            reverse=True,
        )[:20],
        "flows_without_fault_paths": [f["id"] for f in flows.values() if f.get("total_connectors", 0) > 0 and f.get("fault_paths", 0) == 0],
        "triggers_without_test_coverage_hint": None,  # left for enrichment pass; needs code-coverage API data
        "never_cleared_static_collections": never_cleared_statics,
        "fields_with_high_risk_writes": sorted({
            field for field, writers in field_touch_out.items() if any(w["risk"] == "high" for w in writers)
        }),
    }
    stats["most_referenced_objects"] = [
        {"object": o, "touches": t} for o, t in stats["most_referenced_objects"]
    ]

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "org_index.json"), org_index)
    write_json(os.path.join(out_dir, "object_touch_map.json"), object_touch_out)
    write_json(os.path.join(out_dir, "call_graph.json"), {"calls": call_graph, "called_by": reverse_call_graph_out})
    write_json(os.path.join(out_dir, "field_touch_map.json"), field_touch_out)
    write_json(os.path.join(out_dir, "org_stats.json"), stats)

    high_risk_fields = sum(1 for w in field_touch_out.values() if any(x["risk"] == "high" for x in w))
    print(f"Indexed {len(org_index)} components, {len(object_touch_out)} objects touched, "
          f"{len(field_touch_out)} fields with tracked writes ({high_risk_fields} with a high-risk writer), "
          f"{len(never_cleared_statics)} never-cleared static collections org-wide.")


if __name__ == "__main__":
    main()
