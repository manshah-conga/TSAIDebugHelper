"""
Apex class/trigger extraction -- same heuristics as the standalone
extract_apex.py script, refactored to operate purely in memory: callers
pass in already-fetched source text (from the Salesforce Tooling API, or
anywhere else) and get back a JSON-able card. No file paths, no disk
writes -- this module never touches the filesystem, which is what
guarantees raw Apex source is never persisted by the web app.
"""
import re

from .common import strip_comments, find_objects_referenced, truncate

CLASS_DECL_RE = re.compile(
    r"\b(public|private|global)?\s*(virtual|abstract)?\s*(with sharing|without sharing|inherited sharing)?\s*"
    r"(class|interface|enum)\s+(\w+)", re.IGNORECASE)
EXTENDS_RE = re.compile(r"\bextends\s+([\w\.]+)", re.IGNORECASE)
IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w\.,\s]+?)\s*\{", re.IGNORECASE)
SOQL_RE = re.compile(r"\[\s*SELECT\b.*?\]", re.IGNORECASE | re.DOTALL)
SOQL_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z0-9_]+)", re.IGNORECASE)
DYNAMIC_SOQL_RE = re.compile(
    r"Database\.(query|queryWithBinds|getQueryLocator|countQuery)\s*\(", re.IGNORECASE)
DML_RE = re.compile(
    r"\b(insert|update|delete|upsert|undelete|merge)\s+([A-Za-z_][\w\.\[\]]*)", re.IGNORECASE)
DML_DB_RE = re.compile(
    r"Database\.(insert|update|delete|upsert|undelete|merge|emptyRecycleBin)\s*\(", re.IGNORECASE)
CALLOUT_RE = re.compile(
    r"new\s+HttpRequest\s*\(|\bHttp\s*\(\)\s*\.\s*send|WebServiceCallout\.invoke|"
    r"@future\s*\(\s*callout\s*=\s*true", re.IGNORECASE)
NAMED_CRED_RE = re.compile(r"callout:([A-Za-z0-9_/]+)")
THROW_RE = re.compile(r"throw\s+new\s+([\w\.]+)")
CATCH_RE = re.compile(r"catch\s*\(\s*([\w\.]+)\s+\w+\s*\)")
CUSTOM_EXC_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+Exception\b")
TRIGGER_DECL_RE = re.compile(r"trigger\s+(\w+)\s+on\s+([\w.]+)\s*\(([^)]*)\)", re.IGNORECASE)
CALL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.\w+\s*\(")
BATCHABLE_RE = re.compile(r"implements[^\{]*Database\.Batchable", re.IGNORECASE)

STATIC_COLLECTION_RE = re.compile(
    r"\b(?:public|private|global|protected)?\s*static\s+(final\s+)?(Map|List|Set)\s*<.*?>\s*(\w+)\s*=",
    re.IGNORECASE)
FIELD_WRITE_RE = re.compile(r"\b(\w+)\.([A-Za-z]\w*__c)\s*(?<![!<>=])=(?!=)\s*([^;]+);")
MAP_LOOKUP_RE = re.compile(r"^\s*(\w+)\s*\.\s*get\s*\(")
CONSTRUCTOR_RHS_RE = re.compile(r"^\s*new\s+")


def find_static_mutable_state(code):
    found = {}
    for line in code.split("\n"):
        m = STATIC_COLLECTION_RE.search(line)
        if m:
            name = m.group(3)
            if name not in found:
                found[name] = {"final": bool(m.group(1)), "type": m.group(2)}
    state = []
    for name, info in found.items():
        reassign_count = len(re.findall(r"\b" + re.escape(name) + r"\s*=\s*new\b", code))
        clear_calls = len(re.findall(r"\b" + re.escape(name) + r"\s*\.\s*clear\s*\(\s*\)", code))
        state.append({
            "name": name, "collection_type": info["type"], "is_final": info["final"],
            "cleared_or_reassigned_elsewhere": reassign_count > 1 or clear_calls > 0,
        })
    return state


def find_field_writes(code, static_mutable_names):
    writes = []
    for m in FIELD_WRITE_RE.finditer(code):
        receiver, field, rhs = m.group(1), m.group(2), m.group(3).strip()
        lookup_m = MAP_LOOKUP_RE.match(rhs)
        if lookup_m:
            source, pattern = lookup_m.group(1), "map_lookup"
        elif CONSTRUCTOR_RHS_RE.match(rhs):
            source, pattern = None, "constructor"
        else:
            source, pattern = None, "direct"

        risk, reason = "low", None
        if pattern == "map_lookup":
            if source in static_mutable_names:
                if not static_mutable_names[source]:
                    risk = "high"
                    reason = (f"value comes from '{source}.get(...)', a static collection "
                               f"in this class that is never cleared or reset -- a stale or "
                               f"cross-record value can leak into this field with no exception.")
                else:
                    risk = "medium"
                    reason = (f"value comes from '{source}.get(...)', a static collection in "
                               f"this class -- it does appear to be reset somewhere, but verify "
                               f"the reset covers this code path.")
            else:
                risk = "medium"
                reason = f"value is borrowed from '{source}.get(...)' rather than computed directly for this record."

        writes.append({
            "field": field, "receiver": receiver, "pattern": pattern, "source_map": source,
            "rhs": truncate(rhs, 120), "risk": risk, "reason": reason,
        })
    return writes


def parse_class(name, raw_code, known_objects, all_class_names):
    code = strip_comments(raw_code)
    card = {
        "id": name, "type": "ApexClass", "loc": raw_code.count("\n") + 1, "sharing": None,
        "is_test_class": False, "extends": None, "implements": [], "entry_points": [],
        "soql": [], "dml": [], "callouts": [], "named_credentials": [], "exceptions_thrown": [],
        "exceptions_caught": [], "custom_exceptions_defined": [], "calls_to": [],
        "objects_referenced": [], "static_mutable_state": [], "field_writes": [],
    }

    decl = CLASS_DECL_RE.search(code)
    if decl:
        if decl.group(3):
            card["sharing"] = decl.group(3).lower()
        card["type"] = {"class": "ApexClass", "interface": "ApexInterface", "enum": "ApexEnum"}[decl.group(4).lower()]

    if "@istest" in code.lower() or name.lower().endswith("test"):
        card["is_test_class"] = True

    ext = EXTENDS_RE.search(code)
    if ext:
        card["extends"] = ext.group(1)
    impl = IMPLEMENTS_RE.search(code)
    if impl:
        card["implements"] = [i.strip() for i in impl.group(1).split(",") if i.strip()]

    if BATCHABLE_RE.search(code):
        card["entry_points"].append({"kind": "Batchable", "detail": "Database.Batchable"})
    if re.search(r"implements\s+Queueable", code, re.IGNORECASE):
        card["entry_points"].append({"kind": "Queueable", "detail": "Queueable"})
    if re.search(r"implements\s+Schedulable", code, re.IGNORECASE):
        card["entry_points"].append({"kind": "Schedulable", "detail": "Schedulable"})
    for m in re.finditer(r"@RestResource\s*\(urlMapping\s*=\s*'([^']*)'", code, re.IGNORECASE):
        card["entry_points"].append({"kind": "RestResource", "detail": m.group(1)})
    for kind, pattern in [
        ("AuraEnabled", r"@AuraEnabled[^\n]*\n\s*(?:public|global|private)?\s*(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\("),
        ("InvocableMethod", r"@InvocableMethod[^\n]*\n\s*(?:public|global|private)?\s*(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\("),
        ("future", r"@future[^\n]*\n\s*(?:public|global|private)?\s*(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\("),
        ("RemoteAction", r"@RemoteAction[^\n]*\n\s*(?:public|global|private)?\s*(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\("),
    ]:
        for m in re.finditer(pattern, code):
            card["entry_points"].append({"kind": kind, "detail": m.group(1) + "()"})

    for m in SOQL_RE.finditer(code):
        snippet = m.group(0)
        obj_m = SOQL_FROM_RE.search(snippet)
        card["soql"].append({"object": obj_m.group(1) if obj_m else None, "snippet": truncate(snippet, 160)})
    if DYNAMIC_SOQL_RE.search(code):
        card["soql"].append({"object": None, "snippet": "dynamic SOQL via Database.query/queryWithBinds/getQueryLocator"})

    seen_dml = set()
    for m in DML_RE.finditer(code):
        op, target = m.group(1).lower(), m.group(2)
        if (op, target) not in seen_dml:
            seen_dml.add((op, target))
            card["dml"].append({"operation": op, "target": target})
    if DML_DB_RE.search(code):
        for m in DML_DB_RE.finditer(code):
            op = m.group(1).lower()
            if ("Database." + op, None) not in [(d.get("operation"), d.get("target")) for d in card["dml"]]:
                card["dml"].append({"operation": "Database." + op, "target": None})

    if CALLOUT_RE.search(code):
        card["callouts"].append({"mechanism": "HTTP callout (HttpRequest/@future callout)"})
    for m in NAMED_CRED_RE.finditer(code):
        card["named_credentials"].append(m.group(1))

    card["exceptions_thrown"] = sorted(set(THROW_RE.findall(code)))
    card["exceptions_caught"] = sorted(set(CATCH_RE.findall(code)))
    card["custom_exceptions_defined"] = sorted(set(CUSTOM_EXC_RE.findall(code)))

    calls = set()
    for m in CALL_RE.finditer(code):
        callee = m.group(1)
        if callee != name and callee in all_class_names:
            calls.add(callee)
    card["calls_to"] = sorted(calls)
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names)

    return card


def parse_trigger(name, raw_code, known_objects, all_class_names):
    code = strip_comments(raw_code)
    card = {
        "id": name, "type": "ApexTrigger", "loc": raw_code.count("\n") + 1, "object": None,
        "events": [], "entry_points": [], "soql": [], "dml": [], "callouts": [],
        "exceptions_thrown": [], "exceptions_caught": [], "calls_to": [], "objects_referenced": [],
        "static_mutable_state": [], "field_writes": [],
    }
    m = TRIGGER_DECL_RE.search(code)
    if m:
        card["object"] = m.group(2)
        card["events"] = [e.strip() for e in m.group(3).split(",")]

    for m2 in SOQL_RE.finditer(code):
        snippet = m2.group(0)
        obj_m = SOQL_FROM_RE.search(snippet)
        card["soql"].append({"object": obj_m.group(1) if obj_m else None, "snippet": truncate(snippet, 160)})

    seen_dml = set()
    for m2 in DML_RE.finditer(code):
        op, target = m2.group(1).lower(), m2.group(2)
        if (op, target) not in seen_dml:
            seen_dml.add((op, target))
            card["dml"].append({"operation": op, "target": target})

    if CALLOUT_RE.search(code):
        card["callouts"].append({"mechanism": "HTTP callout"})

    card["exceptions_thrown"] = sorted(set(THROW_RE.findall(code)))
    card["exceptions_caught"] = sorted(set(CATCH_RE.findall(code)))

    calls = set()
    for m2 in CALL_RE.finditer(code):
        callee = m2.group(1)
        if callee in all_class_names:
            calls.add(callee)
    card["calls_to"] = sorted(calls)
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names)

    if card["object"]:
        card["entry_points"].append({"kind": "Trigger", "detail": f"{card['object']} ({', '.join(card['events'])})"})
    return card
