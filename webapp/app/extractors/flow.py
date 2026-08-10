"""
Flow extraction from the Salesforce Tooling API's JSON metadata
representation of a Flow (GET /tooling/sobjects/Flow/<id> -> the
"Metadata" property), instead of the raw .flow XML the standalone
extract_flow.py script parses with ElementTree.

IMPORTANT / not yet verified live: this session had no real Salesforce
access token to fetch an actual Flow through the Tooling API, so the
field names below (start.object, start.triggerType, decisions,
recordCreates, actionCalls, subflows, faultConnector, ...) are based on
Salesforce's documented Flow metadata schema, which the Tooling API JSON
representation mirrors 1:1 with the XML element names as JSON keys. On
first real use, if a shape mismatch shows up, it will only affect this
one file (every access below is defensive .get()-based, so a wrong key
degrades to a thinner card instead of crashing the whole org fetch) --
report any mismatch and it's a quick fix here, not a redesign.
"""

ELEMENT_ARRAY_KEYS = [
    "decisions", "assignments", "recordCreates", "recordUpdates",
    "recordLookups", "recordDeletes", "actionCalls", "subflows",
    "screens", "loops", "waits", "collectionProcessors",
]

# processType values that mean "this Flow is really a Process Builder process"
PROCESS_BUILDER_TYPES = {"workflow", "invocableprocess"}


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _looks_like_field(tok):
    """Heuristic: does this final path segment look like a Salesforce field
    API name? Custom fields end in __c (and relationship/namespace fields
    contain __); standard fields are capitalised identifiers."""
    if not tok or not isinstance(tok, str):
        return False
    return tok.endswith("__c") or "__" in tok or (tok[:1].isupper() and tok.isidentifier())


def _describe_value(v):
    """Condense a Flow value node (inputAssignments/assignmentItems 'value')
    into a short {kind, value} so the field-write is checkable without
    keeping the whole metadata blob."""
    if not isinstance(v, dict):
        return {"kind": "literal", "value": v} if v is not None else None
    for k in ("stringValue", "numberValue", "booleanValue", "dateValue", "dateTimeValue"):
        if k in v:
            return {"kind": k, "value": v[k]}
    if "elementReference" in v:
        return {"kind": "reference", "value": v["elementReference"]}
    # formulaExpression, apexValue, sobjectValue, etc.
    key = next(iter(v.keys()), None)
    return {"kind": key or "expression", "value": v.get(key) if key else None}


def _extract_field_writes(metadata, start_object):
    """Name the fields this flow writes, so field_touch_map can index flows
    (and Process Builder processes) as writers -- not just Apex. Covers the
    high-signal shapes: record-update / record-create elements'
    inputAssignments, and assignment elements that target $Record.<Field> or
    a record variable that is later persisted by a record-update/create.

    Best-effort and defensive: unknown shapes are skipped, never crash. PB
    (processType Workflow/InvocableProcess) compiles to the same flow
    metadata, so the same passes apply."""
    writes = []

    def add(field, obj, element_type, element_name, value, confidence):
        if field and _looks_like_field(field):
            writes.append({
                "field": field, "object": obj, "element_type": element_type,
                "element_name": element_name, "value": value, "confidence": confidence,
            })

    # Which record variables actually get persisted (so an assignment writing
    # into one counts as a real field write). $Record is always persisted for
    # record-triggered flows.
    persisted_refs = {"$Record"}

    for key, etype in (("recordUpdates", "recordUpdate"), ("recordCreates", "recordCreate")):
        for el in _as_list(metadata.get(key)):
            if not isinstance(el, dict):
                continue
            ref = el.get("inputReference") or ""
            if ref:
                persisted_refs.add(ref.split(".")[0])
            if el.get("name"):
                persisted_refs.add(el["name"])  # recordCreate stores into its own element name
            obj = el.get("object")
            if not obj and ref.startswith("$Record"):
                obj = start_object
            for a in _as_list(el.get("inputAssignments")):
                if isinstance(a, dict):
                    add(a.get("field"), obj, etype, el.get("name"), _describe_value(a.get("value")), "high")

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
                add(field, start_object, "assignment", el.get("name"), _describe_value(it.get("value")), "high")
            elif head in persisted_refs:
                add(field, None, "assignment", el.get("name"), _describe_value(it.get("value")), "medium")
    return writes


def _count_connectors(node, counts):
    """Walk the metadata dict/list recursively counting 'connector' and
    'faultConnector' keys, the same fault-path signal the XML version
    computes via ElementTree.iter()."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "faultConnector":
                counts["fault"] += 1
            elif k == "connector":
                counts["total"] += 1
            _count_connectors(v, counts)
    elif isinstance(node, list):
        for item in node:
            _count_connectors(item, counts)


def parse_flow(name, metadata, api_version=None):
    process_type = metadata.get("processType")
    mechanism = "Process Builder" if (process_type or "").lower() in PROCESS_BUILDER_TYPES else "Flow"
    card = {
        "id": name, "type": "Flow", "label": metadata.get("label"),
        "processType": process_type, "mechanism": mechanism, "apiVersion": api_version,
        "start_object": None, "trigger_type": None, "record_trigger_type": None,
        "element_counts": {}, "apex_actions_called": [], "subflows_called": [],
        "field_writes": [], "fields_written": [],
        "fault_paths": 0, "total_connectors": 0,
    }

    start = metadata.get("start") or {}
    card["start_object"] = start.get("object")
    card["trigger_type"] = start.get("triggerType")
    card["record_trigger_type"] = start.get("recordTriggerType")

    counts = {}
    for key in ELEMENT_ARRAY_KEYS:
        items = metadata.get(key) or []
        if isinstance(items, dict):  # a single element serializes as an object, not a list, in some API versions
            items = [items]
        if items:
            counts[key] = len(items)
        if key == "actionCalls":
            for a in items:
                if (a.get("actionType") or "").lower() == "apex" and a.get("actionName"):
                    card["apex_actions_called"].append(a["actionName"])
        if key == "subflows":
            for s in items:
                if s.get("flowName"):
                    card["subflows_called"].append(s["flowName"])
    card["element_counts"] = counts

    card["field_writes"] = _extract_field_writes(metadata, card["start_object"])
    card["fields_written"] = sorted({w["field"] for w in card["field_writes"]})

    connector_counts = {"fault": 0, "total": 0}
    _count_connectors(metadata, connector_counts)
    card["fault_paths"] = connector_counts["fault"]
    card["total_connectors"] = connector_counts["total"]

    return card
