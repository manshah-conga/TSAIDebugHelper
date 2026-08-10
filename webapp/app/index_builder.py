"""
Merges apex/flow/lwc component cards into the same knowledgebase shape
the standalone build_index.py produces: org_index, object_touch_map,
call_graph, field_touch_map, org_stats. Pure function over in-memory
dicts -- this is what gets persisted to data/orgs/<org_id>/knowledge_base/,
nothing upstream of it (the raw fetched source) ever is.
"""
from collections import defaultdict


def _fmt_value(value):
    """Render a {kind, value} value node (from a flow/workflow field write)
    into a short human-checkable string, or None."""
    if not isinstance(value, dict):
        return value if value is not None else None
    return f"{value.get('value')}" if value.get("value") is not None else value.get("kind")


def build_index(apex, flows, lwc, workflow=None):
    workflow = workflow or {}
    org_index = {}
    org_index.update(apex)
    org_index.update(flows)
    org_index.update(lwc)
    org_index.update(workflow)

    object_touch = defaultdict(lambda: {
        "read": set(), "write": set(), "trigger": set(), "flow": set(), "workflow": set()})
    for cid, card in apex.items():
        if card["type"] not in ("ApexClass", "ApexTrigger"):
            continue
        for obj in card.get("objects_referenced", []):
            object_touch[obj]["read"].add(cid)
            if card.get("dml"):
                object_touch[obj]["write"].add(cid)
        if card["type"] == "ApexTrigger" and card.get("object"):
            object_touch[card["object"]]["trigger"].add(cid)
    for cid, card in flows.items():
        if card.get("start_object"):
            object_touch[card["start_object"]]["flow"].add(cid)
        for w in card.get("field_writes", []):
            if w.get("object"):
                object_touch[w["object"]]["flow"].add(cid)
    for cid, card in workflow.items():
        if card.get("object"):
            object_touch[card["object"]]["workflow"].add(cid)
    object_touch_out = {
        obj: {k: sorted(v) for k, v in touches.items() if v}
        for obj, touches in sorted(object_touch.items())
    }

    call_graph, reverse_call_graph = {}, defaultdict(set)
    for cid, card in apex.items():
        calls = card.get("calls_to", [])
        call_graph[cid] = calls
        for callee in calls:
            reverse_call_graph[callee].add(cid)
    reverse_call_graph_out = {k: sorted(v) for k, v in reverse_call_graph.items()}

    # Field writers now come from THREE mechanisms, not just Apex:
    #   - Apex classes/triggers (risk high/medium/low from static analysis)
    #   - Flows and Process Builder (recordUpdate/recordCreate/assignment)
    #   - Workflow Rule & Approval Process field updates
    # Declarative writers get risk "declarative" (they run, but carry none of
    # the Apex-specific static-state risk signals); they sort after Apex
    # writers so a high-risk Apex writer still surfaces first.
    risk_rank = {"high": 0, "medium": 1, "low": 2, "declarative": 3}
    field_touch = defaultdict(list)
    never_cleared_statics = []
    for cid, card in apex.items():
        for s in card.get("static_mutable_state", []):
            if not s.get("cleared_or_reassigned_elsewhere"):
                never_cleared_statics.append({"component": cid, "field": s["name"], "type": s["collection_type"]})
        for w in card.get("field_writes", []):
            field_touch[w["field"]].append({
                "component": cid, "mechanism": "Apex", "risk": w["risk"], "pattern": w["pattern"],
                "source_map": w.get("source_map"), "object": None,
                "reason": w.get("reason"), "example": w.get("rhs"),
            })
    for cid, card in flows.items():
        mech = card.get("mechanism", "Flow")
        label = card.get("label") or cid
        for w in card.get("field_writes", []):
            conf = w.get("confidence")
            field_touch[w["field"]].append({
                "component": cid, "mechanism": mech, "risk": "declarative",
                "pattern": w.get("element_type"), "source_map": None, "object": w.get("object"),
                "reason": (f"written by {mech} '{label}' in a {w.get('element_type')} element"
                           + (f" (match confidence: {conf})" if conf else "")),
                "example": _fmt_value(w.get("value")), "confidence": conf,
            })
    for cid, card in workflow.items():
        op = card.get("operation")
        field_touch[card["field"]].append({
            "component": cid, "mechanism": card.get("mechanism", "Workflow/Approval field update"),
            "risk": "declarative", "pattern": "field_update", "source_map": None,
            "object": card.get("object"),
            "reason": (f"written by a Workflow Rule / Approval Process field update '{cid}'"
                       + (f" (operation: {op})" if op else "")),
            "example": _fmt_value(card.get("value")),
        })
    field_touch_out = {}
    for field, writers in field_touch.items():
        seen, deduped = set(), []
        for w in sorted(writers, key=lambda w: risk_rank.get(w["risk"], 9)):
            key = (w["component"], w["mechanism"], w["risk"], w["source_map"])
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
            "flows": len(flows), "lwc_components": len(lwc),
            "process_builder_processes": sum(1 for c in flows.values() if c.get("mechanism") == "Process Builder"),
            "workflow_field_updates": len(workflow),
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
        "most_referenced_objects": [
            {"object": o, "touches": t} for o, t in sorted(
                object_touch_out.items(), key=lambda kv: sum(len(v) for v in kv[1].values()), reverse=True,
            )[:20]
        ],
        "flows_without_fault_paths": [f["id"] for f in flows.values() if f.get("total_connectors", 0) > 0 and f.get("fault_paths", 0) == 0],
        "never_cleared_static_collections": never_cleared_statics,
        "fields_with_high_risk_writes": sorted({
            field for field, writers in field_touch_out.items() if any(w["risk"] == "high" for w in writers)
        }),
        "fields_written_by_declarative_automation": sorted({
            field for field, writers in field_touch_out.items()
            if any(w["risk"] == "declarative" for w in writers)
        }),
    }

    return {
        "org_index": org_index,
        "object_touch_map": object_touch_out,
        "call_graph": {"calls": call_graph, "called_by": reverse_call_graph_out},
        "field_touch_map": field_touch_out,
        "org_stats": stats,
    }
