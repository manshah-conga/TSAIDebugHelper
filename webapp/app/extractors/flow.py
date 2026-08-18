"""
Flow extraction from the Salesforce Tooling API's JSON metadata
representation of a Flow (GET /tooling/sobjects/Flow/<id> -> the
"Metadata" property).

Schema v3 (see ../schema.py and the v3 proposal): in addition to the v2
summary fields, the flow card now carries the entry-criteria block
(`trigger`), the element graph (`elements` + derived `graph`), structured
`action_calls`, version/activation state, and the unified value model. All
of this is derived from the metadata dict -- still no raw source on disk.

Every access is defensive (.get()/_as_list): an unexpected shape yields a
thinner card, never a crash, so one odd flow can't abort a whole org fetch.
"""
from .. import schema

ELEMENT_ARRAY_KEYS = [
    "decisions", "assignments", "recordCreates", "recordUpdates",
    "recordLookups", "recordDeletes", "actionCalls", "subflows",
    "screens", "loops", "waits", "collectionProcessors",
]

# Elements that perform DML/callout work and therefore *should* have a fault path.
FAULT_CAPABLE_TYPES = {"recordUpdate", "recordCreate", "recordDelete", "recordLookup", "actionCall"}

PROCESS_BUILDER_TYPES = {"workflow", "invocableprocess"}


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _looks_like_field(tok):
    if not tok or not isinstance(tok, str):
        return False
    return tok.endswith("__c") or "__" in tok or (tok[:1].isupper() and tok.isidentifier())


def _target(connector):
    if isinstance(connector, dict):
        return connector.get("targetReference")
    return None


def _conditions(node):
    """Parse a decision-rule / filter condition list into the unified model."""
    out = []
    for c in _as_list(node):
        if isinstance(c, dict):
            out.append({
                "field": c.get("field") or c.get("leftValueReference"),
                "operator": c.get("operator"),
                "value": schema.flow_value(c.get("value") or c.get("rightValue")),
            })
    return out


# ---------- field writes (kept from v2, now on the unified value model) ----------

def _extract_field_writes(metadata, start_object):
    writes = []

    def add(field, obj, element_type, element_name, value, confidence, basis):
        if field and _looks_like_field(field):
            writes.append({
                "field": field, "object": obj, "element_type": element_type,
                "element_name": element_name, "value": value,
                "confidence": confidence, "confidence_basis": basis,
            })

    persisted_refs = {"$Record"}
    for key, etype in (("recordUpdates", "recordUpdate"), ("recordCreates", "recordCreate")):
        for el in _as_list(metadata.get(key)):
            if not isinstance(el, dict):
                continue
            ref = el.get("inputReference") or ""
            if ref:
                persisted_refs.add(ref.split(".")[0])
            if el.get("name"):
                persisted_refs.add(el["name"])
            obj = el.get("object")
            if not obj and ref.startswith("$Record"):
                obj = start_object
            for a in _as_list(el.get("inputAssignments")):
                if isinstance(a, dict):
                    add(a.get("field"), obj, etype, el.get("name"),
                        schema.flow_value(a.get("value")), "high", "direct_field_reference")

    for el in _as_list(metadata.get("assignments")):
        if not isinstance(el, dict):
            continue
        for it in _as_list(el.get("assignmentItems")):
            if not isinstance(it, dict):
                continue
            ref = it.get("assignToReference") or ""
            if "." not in ref:
                continue
            head = ref.split(".")[0]
            field = ref.rsplit(".", 1)[-1]
            if head.startswith("$Record"):
                add(field, start_object, "assignment", el.get("name"),
                    schema.flow_value(it.get("value")), "high", "direct_field_reference")
            elif head in persisted_refs:
                add(field, None, "assignment", el.get("name"),
                    schema.flow_value(it.get("value")), "medium", "via_record_variable")
    return writes


# ---------- trigger / entry criteria (§5.2) ----------

def _trigger_block(metadata, start):
    # P-01: distinguish "absent from metadata" from "collected as false".
    rc = start.get("doesRequireRecordChangedToMeetCriteria", "__MISSING__")
    requires_change = rc if rc not in ("__MISSING__", None) else "not_present_in_metadata"
    trig = {
        "type": start.get("triggerType"),
        "record_trigger_type": start.get("recordTriggerType"),
        "object": start.get("object"),
        "filter_logic": start.get("filterLogic") or ("and" if start.get("filters") else None),
        "filter_formula": start.get("filterFormula"),
        "filters": _conditions(start.get("filters")),
        "requires_change_to_meet_criteria": requires_change,
        "run_in_mode": metadata.get("runInMode") or start.get("runInMode"),
        "scheduled_paths": [],
    }
    for sp in _as_list(start.get("scheduledPaths")):
        if isinstance(sp, dict):
            trig["scheduled_paths"].append({
                "name": sp.get("name") or sp.get("label"),
                "offset_number": sp.get("offsetNumber"),
                "offset_unit": sp.get("offsetUnit"),
                "base_field": sp.get("recordField"),
                "time_source": sp.get("timeSource"),
            })
    return trig


# ---------- elements + graph (§5.4-5.6) ----------

def _element_common(el, etype):
    return {"name": el.get("name"), "label": el.get("label"), "type": etype,
            "next": _target(el.get("connector")), "fault_target": _target(el.get("faultConnector"))}


def _extract_elements(metadata, start_object=None):
    elements = []
    connectors = 0
    fault_paths = 0

    for el in _as_list(metadata.get("decisions")):
        if not isinstance(el, dict):
            continue
        e = {"name": el.get("name"), "label": el.get("label"), "type": "decision",
             "rules": [], "default_target": _target(el.get("defaultConnector")), "fault_target": None}
        for r in _as_list(el.get("rules")):
            if isinstance(r, dict):
                e["rules"].append({
                    "name": r.get("name"), "label": r.get("label"),
                    "condition_logic": r.get("conditionLogic") or "and",
                    "conditions": _conditions(r.get("conditions")),
                    "target": _target(r.get("connector")),
                })
        elements.append(e)

    for el in _as_list(metadata.get("recordUpdates")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "recordUpdate")
        ref = el.get("inputReference")
        # Q-07: a $Record-based update has no explicit object; derive it from
        # the flow's start object so consumers don't have to do the join.
        e["target_object"] = el.get("object") or (start_object if (ref or "").startswith("$Record") else None)
        e["target_reference"] = ref
        elements.append(e)
    for el in _as_list(metadata.get("recordCreates")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "recordCreate")
        e["target_object"] = el.get("object")
        elements.append(e)
    for el in _as_list(metadata.get("recordDeletes")):
        if not isinstance(el, dict):
            continue
        elements.append(_element_common(el, "recordDelete"))

    for el in _as_list(metadata.get("recordLookups")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "recordLookup")
        e["object"] = el.get("object")
        e["filters"] = _conditions(el.get("filters"))
        e["get_first_only"] = el.get("getFirstRecordOnly")
        e["stores_into"] = el.get("outputReference")
        elements.append(e)

    for el in _as_list(metadata.get("assignments")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "assignment")
        e["assignments"] = [
            {"to": it.get("assignToReference"), "operator": it.get("operator"),
             "from": schema.flow_value(it.get("value"))}
            for it in _as_list(el.get("assignmentItems")) if isinstance(it, dict)
        ]
        elements.append(e)

    for el in _as_list(metadata.get("loops")):
        if not isinstance(el, dict):
            continue
        e = {"name": el.get("name"), "label": el.get("label"), "type": "loop",
             "collection": el.get("collectionReference"), "iteration_order": el.get("iterationOrder"),
             "next": _target(el.get("nextValueConnector")),
             "no_more_values_target": _target(el.get("noMoreValuesConnector")), "fault_target": None}
        elements.append(e)

    for el in _as_list(metadata.get("actionCalls")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "actionCall")
        e["action_type"] = el.get("actionType")
        e["action_name"] = el.get("actionName")
        elements.append(e)

    for el in _as_list(metadata.get("subflows")):
        if not isinstance(el, dict):
            continue
        e = _element_common(el, "subflow")
        e["flow_name"] = el.get("flowName")
        elements.append(e)

    for el in _as_list(metadata.get("screens")):
        if isinstance(el, dict):
            elements.append(_element_common(el, "screen"))
    for el in _as_list(metadata.get("waits")):
        if isinstance(el, dict):
            elements.append(_element_common(el, "wait"))

    # count connectors / fault paths from the structured elements (matches v2 scalar)
    for e in elements:
        for tgt in _outgoing(e):
            if tgt:
                connectors += 1
        if e.get("fault_target"):
            fault_paths += 1
    return elements, connectors, fault_paths


def _outgoing(e):
    """All target references leaving an element (for the adjacency graph)."""
    targets = []
    if e.get("type") == "decision":
        for r in e.get("rules", []):
            targets.append(r.get("target"))
        targets.append(e.get("default_target"))
    elif e.get("type") == "loop":
        targets.append(e.get("next"))
        targets.append(e.get("no_more_values_target"))
    else:
        targets.append(e.get("next"))
    targets.append(e.get("fault_target"))
    return [t for t in targets if t]


def _build_graph(elements, start_element):
    by_name = {e["name"]: e for e in elements if e.get("name")}
    adj = {name: [] for name in by_name}
    indeg = {name: 0 for name in by_name}
    for name, e in by_name.items():
        for tgt in _outgoing(e):
            if tgt in by_name:
                adj[name].append(tgt)
    for name in adj:
        for tgt in adj[name]:
            indeg[tgt] += 1

    # reachability from start
    reachable, stack = set(), [start_element] if start_element in by_name else []
    while stack:
        n = stack.pop()
        if n in reachable:
            continue
        reachable.add(n)
        stack.extend(adj.get(n, []))
    unreachable = sorted(n for n in by_name if n not in reachable)

    # Kahn topological sort (also detects cycles)
    from collections import deque
    indeg2 = dict(indeg)
    q = deque([n for n in by_name if indeg2[n] == 0])
    topo = []
    while q:
        n = q.popleft()
        topo.append(n)
        for m in adj[n]:
            indeg2[m] -= 1
            if indeg2[m] == 0:
                q.append(m)
    has_cycles = len(topo) < len(by_name)

    without_fault = sorted(
        e["name"] for e in elements
        if e.get("type") in FAULT_CAPABLE_TYPES and not e.get("fault_target") and e.get("name"))

    # longest path (depth) only meaningful when acyclic
    max_depth = None
    if not has_cycles and start_element in by_name:
        depth = {n: 0 for n in by_name}
        for n in topo:
            for m in adj[n]:
                depth[m] = max(depth[m], depth[n] + 1)
        max_depth = max(depth.values()) if depth else 0

    return {
        "start_element": start_element,
        "topological_order": topo if not has_cycles else [],
        "has_cycles": has_cycles,
        "unreachable_elements": unreachable,
        "elements_without_fault_path": without_fault,
        "max_depth": max_depth,
    }


# ---------- action calls (§5.3) ----------

def _action_calls(metadata):
    out = []
    for el in _as_list(metadata.get("actionCalls")):
        if not isinstance(el, dict):
            continue
        atype = (el.get("actionType") or "").strip() or None
        entry = {
            "element": el.get("name"),
            "action_type": atype,
            "action_name": el.get("actionName"),
            "inputs": [
                {"name": p.get("name"), "value": schema.flow_value(p.get("value"))}
                for p in _as_list(el.get("inputParameters")) if isinstance(p, dict)
            ],
            "outputs": [p.get("name") for p in _as_list(el.get("outputParameters")) if isinstance(p, dict)],
            "fault_target": _target(el.get("faultConnector")),
        }
        if atype == "apex":
            # resolved_method is filled by the index pass (P1: no cross-file work here)
            entry["resolved_method"] = None
            entry["resolution"] = "pending"
        else:
            entry["resolution"] = "standard_action"
        out.append(entry)
    return out


# ---------- objects touched ----------

def _objects_touched(metadata, start_object, field_writes):
    touched = {}

    def mark(obj, read=False, write=False):
        if not obj:
            return
        t = touched.setdefault(obj, {"object": obj, "reads": False, "writes": False})
        t["reads"] = t["reads"] or read
        t["writes"] = t["writes"] or write

    if start_object:
        mark(start_object, read=True)
    for el in _as_list(metadata.get("recordLookups")):
        if isinstance(el, dict):
            mark(el.get("object"), read=True)
    for key in ("recordUpdates", "recordCreates", "recordDeletes"):
        for el in _as_list(metadata.get(key)):
            if isinstance(el, dict):
                obj = el.get("object")
                if not obj and (el.get("inputReference") or "").startswith("$Record"):
                    obj = start_object
                mark(obj, write=True)
    for w in field_writes:
        mark(w.get("object"), write=True)
    return list(touched.values())


# ---------- top-level ----------

def parse_flow(name, metadata, api_version=None, version_info=None, namespace_prefix=None):
    process_type = metadata.get("processType")
    mechanism = "Process Builder" if (process_type or "").lower() in PROCESS_BUILDER_TYPES else "Flow"
    start = metadata.get("start") or {}
    start_object = start.get("object")

    card = {
        "id": name, "type": "Flow",
        **schema.envelope(api_version),
        **schema.namespace_fields(namespace_prefix, name),
        "label": metadata.get("label"),
        "description": metadata.get("description"),
        "processType": process_type, "mechanism": mechanism, "apiVersion": api_version,
        "start_object": start_object,
        "trigger_type": start.get("triggerType"),
        "record_trigger_type": start.get("recordTriggerType"),
    }

    # version / activation state (§5.1) -- supplied by the fetch layer
    vi = version_info or {}
    card["version_number"] = vi.get("version_number")
    card["status"] = vi.get("status")
    card["is_active_version"] = vi.get("is_active_version")
    # Q-02 / P3: we fetch ONLY the active version, so we cannot list siblings.
    # `null` + version_capture says that honestly; `[]` would falsely claim
    # "no other versions exist" on e.g. a version-60 flow.
    card["sibling_versions"] = vi.get("sibling_versions")  # None unless enumerated
    card["version_capture"] = "active_version_only" if vi else "not_collected"

    card["trigger"] = _trigger_block(metadata, start)

    elements, connectors, fault_paths = _extract_elements(metadata, start_object)
    card["elements"] = elements
    card["start_element"] = _target(start.get("connector"))
    card["graph"] = _build_graph(elements, card["start_element"])
    card["total_connectors"] = connectors
    card["fault_paths"] = fault_paths

    card["action_calls"] = _action_calls(metadata)
    card["apex_actions_called"] = [a["action_name"] for a in card["action_calls"]
                                   if a.get("action_type") == "apex" and a.get("action_name")]
    card["subflows_called"] = [e.get("flow_name") for e in elements
                               if e.get("type") == "subflow" and e.get("flow_name")]

    card["field_writes"] = _extract_field_writes(metadata, start_object)
    card["fields_written"] = sorted({w["field"] for w in card["field_writes"]})
    card["objects_touched"] = _objects_touched(metadata, start_object, card["field_writes"])

    counts = {}
    for key in ELEMENT_ARRAY_KEYS:
        items = _as_list(metadata.get(key))
        if items:
            counts[key] = len(items)
    card["element_counts"] = counts

    # D-02.4: surface how many value nodes could not be parsed.
    card["parse_stats"] = schema.count_value_nodes(card)
    return card
