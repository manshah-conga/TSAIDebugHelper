"""
Index pass (schema v3). Merges apex/flow/lwc/workflow cards into the
knowledgebase and, in a single post-extraction pass, computes everything
that needs to see more than one card: the reverse-call (inbound) index,
per-object entry points, the field-writers index, flow action-call method
resolution (§5.3), and the derived flags (§9).

Design principle P1 holds: extraction stayed per-file; all cross-component
resolution happens here.
"""
import re
from collections import defaultdict

FAULT_CAPABLE = {"recordUpdate", "recordCreate", "recordDelete", "recordLookup", "actionCall"}
FIELD_NS_RE = re.compile(r"^([A-Za-z0-9]+)__.+__c$")


def _fmt_value(value):
    if not isinstance(value, dict):
        return value if value is not None else None
    if value.get("is_blank_assignment"):
        return "(blank)"
    return value.get("value") if value.get("value") is not None else (value.get("expression") or value.get("kind"))


def _field_namespace(field):
    """Namespace prefix of a field API name, or None. 'Apttus__Status__c' ->
    'Apttus'; a plain custom field 'Foo__c' -> None."""
    m = FIELD_NS_RE.match(field or "")
    return m.group(1) if m else None


def _call_targets(card):
    """calls_to may be v3 (list of dicts) or legacy (list of strings)."""
    out = []
    for c in card.get("calls_to", []):
        if isinstance(c, dict):
            if c.get("target"):
                out.append(c["target"])
        elif isinstance(c, str):
            out.append(c)
    return out


# ---------- flow action-call resolution (§5.3) ----------

def _resolve_action_calls(flows, org_index):
    for cid, card in flows.items():
        for ac in card.get("action_calls", []):
            if ac.get("resolution") != "pending":
                continue
            cls = ac.get("action_name")
            target = org_index.get(cls) if cls else None
            if target is None:
                ac["resolved_method"] = None
                ac["resolution"] = "class_not_in_kb"
                ac["resolution_note"] = "referenced class not in knowledgebase (managed package or extraction gap)"
                continue
            invocables = [e["detail"].rstrip("()") for e in target.get("entry_points", [])
                          if e.get("kind") == "InvocableMethod"]
            if len(invocables) == 1:
                ac["resolved_method"] = invocables[0]
                ac["resolution"] = "unique_invocable_in_class"
                ac["resolution_note"] = "Salesforce permits one @InvocableMethod per class, so class resolves 1:1"
            elif len(invocables) > 1:
                ac["resolved_method"] = None
                ac["resolution"] = "ambiguous"
            else:
                ac["resolved_method"] = None
                ac["resolution"] = "class_not_in_kb"
                ac["resolution_note"] = "class in KB but no @InvocableMethod found"


# ---------- inbound / entry-point indexes (§7) ----------

_ASYNC_MECHANISMS = {"System.enqueueJob", "Database.executeBatch", "System.schedule", "System.scheduleBatch"}


def _build_inbound(apex, flows, lwc=None):
    inbound = defaultdict(lambda: {"called_by": []})
    for cid, card in apex.items():
        # Async dispatches first: they carry the method, line and delay, which
        # is what a 'where is this Queueable enqueued from?' answer needs.
        dispatched = set()
        for d in card.get("async_dispatches", []) or []:
            tgt = d.get("target")
            if not tgt or tgt == cid:
                continue
            dispatched.add(tgt)
            entry = {"id": cid, "type": card["type"], "via": d.get("mechanism"),
                     "method": d.get("method"), "line": d.get("line"),
                     "in_loop": d.get("in_loop")}
            for k in ("delay_minutes", "scope_size", "job_name", "cron", "inner_class"):
                if d.get(k) is not None:
                    entry[k] = d[k]
            inbound[tgt]["called_by"].append(entry)
        for c in card.get("calls_to", []):
            if isinstance(c, str):
                inbound[c]["called_by"].append({"id": cid, "type": card["type"], "via": "method_call"})
                continue
            tgt = c.get("target")
            if not tgt:
                continue
            vias = [v for v in (c.get("via") or ["method_call"]) if v not in _ASYNC_MECHANISMS]
            if tgt in dispatched:
                # the `new X(` that built the job is the same edge as the dispatch
                vias = [v for v in vias if v != "constructor"]
            if not vias:
                continue
            entry = {"id": cid, "type": card["type"], "via": vias[0] if len(vias) == 1 else ",".join(vias)}
            if c.get("methods_called"):
                entry["methods_called"] = c["methods_called"]
            inbound[tgt]["called_by"].append(entry)
    for cid, card in (lwc or {}).items():
        by_class = defaultdict(list)
        for imp in card.get("apex_methods_imported", []) or []:
            cls, _, meth = imp.partition(".")
            if cls:
                by_class[cls].append(meth)
        for cls, meths in by_class.items():
            inbound[cls]["called_by"].append({"id": cid, "type": "LWC", "via": "lwc_apex_import",
                                              "methods_called": sorted(set(m for m in meths if m))})
    for cid, card in flows.items():
        for ac in card.get("action_calls", []):
            if ac.get("action_type") == "apex" and ac.get("action_name"):
                inbound[ac["action_name"]]["called_by"].append(
                    {"id": cid, "type": "Flow", "via": "actionCall",
                     "element": ac.get("element"), "resolved_method": ac.get("resolved_method")})
        for sub in card.get("subflows_called", []):
            inbound[sub]["called_by"].append({"id": cid, "type": "Flow", "via": "subflow"})
    return dict(inbound)


def _build_entry_points(apex, flows, workflow):
    ep = defaultdict(lambda: {"before_save_flows": [], "after_save_flows": [], "apex_triggers": [],
                              "process_builder": [], "workflow_field_updates": [],
                              "self_referential_automation": []})
    for cid, card in flows.items():
        obj = card.get("start_object")
        if not obj:
            continue
        entry = {"id": cid, "status": card.get("status"),
                 "has_entry_filter": bool(card.get("trigger", {}).get("filters"))}
        tt = (card.get("trigger_type") or "").lower()
        bucket = "before_save_flows" if "beforesave" in tt else "after_save_flows"
        if card.get("mechanism") == "Process Builder":
            ep[obj]["process_builder"].append(entry)
        else:
            ep[obj][bucket].append(entry)
        if "self_referential_update" in card.get("flags", []):
            ep[obj]["self_referential_automation"].append(cid)
    for cid, card in apex.items():
        if card.get("type") == "ApexTrigger" and card.get("object"):
            ep[card["object"]]["apex_triggers"].append({"id": cid, "events": card.get("events", [])})
    for cid, card in workflow.items():
        if card.get("object"):
            ep[card["object"]]["workflow_field_updates"].append(cid)
    return dict(ep)


# ---------- derived flags (§9) ----------

def _flow_flags(card):
    flags = []
    tt = (card.get("trigger_type") or "")
    after_save = "AfterSave" in tt
    start_obj = card.get("start_object")
    elements = card.get("elements", [])
    updates_self = any(
        e.get("type") == "recordUpdate" and (
            e.get("target_object") == start_obj or (e.get("target_reference") or "").startswith("$Record"))
        for e in elements)
    if after_save and updates_self:
        flags.append("self_referential_update")

    filter_fields = {f.get("field") for f in card.get("trigger", {}).get("filters", []) if f.get("field")}
    clears_filter_field = any(
        w.get("field") in filter_fields and isinstance(w.get("value"), dict)
        and (w["value"].get("is_blank_assignment") or w["value"].get("value") in (False, "false"))
        for w in card.get("field_writes", []))
    if filter_fields and clears_filter_field:
        flags.append("loop_guard_detected")

    if ("self_referential_update" in flags and "loop_guard_detected" not in flags
            and card.get("trigger", {}).get("requires_change_to_meet_criteria") is False):
        flags.append("loop_guard_missing")

    comp_ns = card.get("namespace")
    if any(_field_namespace(w.get("field")) and _field_namespace(w.get("field")) != comp_ns
           for w in card.get("field_writes", [])):
        flags.append("writes_managed_package_fields")

    graph = card.get("graph", {})
    if graph.get("elements_without_fault_path"):
        flags.append("no_fault_path")
    if graph.get("unreachable_elements"):
        flags.append("unreachable_elements")
    return flags


def _apex_flags(card):
    flags = []
    if any((c.get("handler", {}) or {}).get("effect") in ("empty", "swallow", "debug_only")
           for c in card.get("exceptions_caught", []) if isinstance(c, dict)):
        flags.append("silent_exception_handler")
    if any(w.get("persistence") == "unpersisted_or_unresolved" for w in card.get("field_writes", [])):
        flags.append("unpersisted_field_write")
    if any(d.get("in_loop") for d in card.get("dml", [])):
        flags.append("dml_in_loop")
    if any(s.get("in_loop") for s in card.get("soql", [])):
        flags.append("soql_in_loop")
    if any(s.get("is_dynamic") for s in card.get("soql", [])):
        flags.append("dynamic_soql")
    if any(d.get("in_loop") for d in card.get("async_dispatches", []) or []):
        # 50 enqueueJob per sync transaction (1 from inside a Queueable),
        # 5 queued/active batch jobs -- a dispatch in a loop is a limit bug.
        flags.append("async_dispatch_in_loop")
    controller = any(e.get("kind") in ("AuraEnabled", "RemoteAction", "RestResource")
                     for e in card.get("entry_points", []))
    if controller and any(s.get("enforces_fls") is False and not s.get("is_dynamic")
                          for s in card.get("soql", [])):
        flags.append("no_fls_enforcement")
    comp_ns = card.get("namespace")
    if any(_field_namespace(w.get("field")) and _field_namespace(w.get("field")) != comp_ns
           for w in card.get("field_writes", [])):
        flags.append("writes_managed_package_fields")
    return flags


# ---------- main ----------

def build_index(apex, flows, lwc, workflow=None, coverage=None):
    workflow = workflow or {}
    org_index = {}
    org_index.update(apex)
    org_index.update(flows)
    org_index.update(lwc)
    org_index.update(workflow)

    _resolve_action_calls(flows, org_index)

    # flags (needs elements/handlers/etc; must run before entry-points uses them)
    for card in flows.values():
        card["flags"] = _flow_flags(card)
    for card in apex.values():
        if card.get("type") in ("ApexClass", "ApexTrigger"):
            card["flags"] = _apex_flags(card)

    # object touch map
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
        for t in card.get("objects_touched", []):
            if t.get("reads"):
                object_touch[t["object"]]["read"].add(cid)
            if t.get("writes"):
                object_touch[t["object"]]["flow"].add(cid)
        if card.get("start_object"):
            object_touch[card["start_object"]]["flow"].add(cid)
    for cid, card in workflow.items():
        if card.get("object"):
            object_touch[card["object"]]["workflow"].add(cid)
    object_touch_out = {obj: {k: sorted(v) for k, v in t.items() if v}
                        for obj, t in sorted(object_touch.items())}

    # call graph (v3 calls_to is list of dicts)
    call_graph, reverse = {}, defaultdict(set)
    for cid, card in apex.items():
        targets = _call_targets(card)
        call_graph[cid] = targets
        for callee in targets:
            reverse[callee].add(cid)
    reverse_out = {k: sorted(v) for k, v in reverse.items()}

    # fields used in flow entry criteria (§7.2 used_in_entry_criteria_of)
    entry_crit = defaultdict(set)
    for cid, card in flows.items():
        for f in card.get("trigger", {}).get("filters", []):
            if f.get("field"):
                entry_crit[f["field"]].add(cid)

    # field touch map (writers by mechanism, enriched per §7.2)
    risk_rank = {"high": 0, "medium": 1, "low": 2, "declarative": 3}
    field_touch = defaultdict(list)
    never_cleared = []
    for cid, card in apex.items():
        for s in card.get("static_mutable_state", []):
            if not s.get("cleared_or_reassigned_elsewhere"):
                never_cleared.append({"component": cid, "field": s["name"], "type": s["collection_type"]})
        for w in card.get("field_writes", []):
            field_touch[w["field"]].append({
                "component": cid, "mechanism": "Apex", "risk": w["risk"], "pattern": w["pattern"],
                "source_map": w.get("source_map"), "object": w.get("object"), "method": w.get("method"),
                "persistence": w.get("persistence"), "reason": w.get("reason"),
                "example": _fmt_value(w.get("rhs")),
            })
    for cid, card in flows.items():
        mech = card.get("mechanism", "Flow")
        label = card.get("label") or cid
        for w in card.get("field_writes", []):
            field_touch[w["field"]].append({
                "component": cid, "mechanism": mech, "risk": "declarative",
                "pattern": w.get("element_type"), "source_map": None, "object": w.get("object"),
                "flow_status": card.get("status"), "is_active_version": card.get("is_active_version"),
                "confidence": w.get("confidence"),
                "reason": f"written by {mech} '{label}' in a {w.get('element_type')} element"
                          + (f" (match confidence: {w.get('confidence')})" if w.get("confidence") else ""),
                "example": _fmt_value(w.get("value")),
            })
    for cid, card in workflow.items():
        field_touch[card["field"]].append({
            "component": cid, "mechanism": card.get("mechanism", "Workflow/Approval field update"),
            "risk": "declarative", "pattern": "field_update", "source_map": None,
            "object": card.get("object"),
            "reason": f"written by a Workflow Rule / Approval Process field update '{cid}'"
                      + (f" (operation: {card.get('operation')})" if card.get("operation") else ""),
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
        entry = {"writers": deduped}
        if field in entry_crit:
            entry["used_in_entry_criteria_of"] = sorted(entry_crit[field])
        field_touch_out[field] = entry

    inbound_index = _build_inbound(apex, flows, lwc)
    entry_points_index = _build_entry_points(apex, flows, workflow)

    # Stamp who starts each async job straight onto the job's own card, so
    # get_component on a Queueable/Batchable/Schedulable answers "where is
    # this invoked from?" without a second lookup.
    async_invocations = {}
    for cid, card in apex.items():
        callers = [c for c in inbound_index.get(cid, {}).get("called_by", [])
                   if c.get("via") in _ASYNC_MECHANISMS]
        for ep in card.get("entry_points", []):
            if ep.get("kind") in ("Queueable", "Batchable", "Schedulable") and not ep.get("inner_class"):
                ep["invoked_by"] = [{k: c[k] for k in ("id", "via", "method", "line", "delay_minutes",
                                                        "scope_size", "cron", "job_name") if c.get(k) is not None}
                                    for c in callers]
        if callers:
            async_invocations[cid] = [{"by": c["id"], "via": c["via"], "method": c.get("method"),
                                       "line": c.get("line")} for c in callers]

    stats = {
        "counts": {
            "apex_classes": sum(1 for c in apex.values() if c["type"] == "ApexClass"),
            "apex_triggers": sum(1 for c in apex.values() if c["type"] == "ApexTrigger"),
            "test_classes": sum(1 for c in apex.values() if c.get("is_test_class")),
            "flows": len(flows), "lwc_components": len(lwc),
            "process_builder_processes": sum(1 for c in flows.values() if c.get("mechanism") == "Process Builder"),
            "workflow_field_updates": len(workflow),
        },
        "coverage": coverage or {},
        "async_job_classes": {
            "batchable": [c["id"] for c in apex.values() if any(e["kind"] == "Batchable" for e in c.get("entry_points", []))],
            "queueable": [c["id"] for c in apex.values() if any(e["kind"] == "Queueable" for e in c.get("entry_points", []))],
            "schedulable": [c["id"] for c in apex.values() if any(e["kind"] == "Schedulable" for e in c.get("entry_points", []))],
            "future": [c["id"] for c in apex.values() if any(e["kind"] == "future" for e in c.get("entry_points", []))],
        },
        # job class -> where it is enqueued / executed / scheduled from
        "async_invocations": async_invocations,
        "integration_points": {
            "classes_with_callouts": [c["id"] for c in apex.values() if c.get("callouts")],
            "named_credentials_referenced": sorted({nc for c in apex.values() for nc in c.get("named_credentials", [])}),
        },
        "custom_exceptions_defined": sorted({e for c in apex.values() for e in c.get("custom_exceptions_defined", [])}),
        "most_referenced_objects": [
            {"object": o, "touches": t} for o, t in sorted(
                object_touch_out.items(), key=lambda kv: sum(len(v) for v in kv[1].values()), reverse=True)[:20]
        ],
        "flows_without_fault_paths": [f["id"] for f in flows.values()
                                      if f.get("graph", {}).get("elements_without_fault_path")],
        "never_cleared_static_collections": never_cleared,
        "fields_with_high_risk_writes": sorted({
            field for field, e in field_touch_out.items() if any(w["risk"] == "high" for w in e["writers"])}),
        "fields_written_by_declarative_automation": sorted({
            field for field, e in field_touch_out.items() if any(w["risk"] == "declarative" for w in e["writers"])}),
        "components_with_flags": {
            cid: card["flags"] for cid, card in {**apex, **flows}.items() if card.get("flags")},
        "customer_authored_counts": {
            "apex": sum(1 for c in apex.values() if c.get("is_customer_authored")),
            "flows": sum(1 for c in flows.values() if c.get("is_customer_authored")),
        },
    }

    return {
        "org_index": org_index,
        "object_touch_map": object_touch_out,
        "call_graph": {"calls": call_graph, "called_by": reverse_out},
        "field_touch_map": field_touch_out,
        "inbound_index": inbound_index,
        "entry_points_index": entry_points_index,
        "org_stats": stats,
    }
