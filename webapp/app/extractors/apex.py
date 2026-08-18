"""
Apex class/trigger extraction (schema v3). Heuristic/regex based -- callers
pass in already-fetched source text and get back a JSON-able card; this
module never touches the filesystem, so raw Apex source is never persisted.

v3 additions over v2: the card envelope + namespace (../schema.py), a
`methods[]` inventory, exception-handler characterisation
(`exceptions_caught[].handler`), structured `soql[]`, DML linkage +
`field_writes[].persistence`, and `calls_to[]` classification. Line numbers
are approximate (computed on the comment-stripped source) and labelled as a
triage aid, not exact positions.
"""
import re

from .. import schema
from .common import strip_comments, find_objects_referenced, truncate

CLASS_DECL_RE = re.compile(
    r"\b(public|private|global)?\s*(virtual|abstract)?\s*(with sharing|without sharing|inherited sharing)?\s*"
    r"(class|interface|enum)\s+(\w+)", re.IGNORECASE)
EXTENDS_RE = re.compile(r"\bextends\s+([\w\.]+)", re.IGNORECASE)
IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w\.,\s]+?)\s*\{", re.IGNORECASE)
SOQL_RE = re.compile(r"\[\s*SELECT\b.*?\]", re.IGNORECASE | re.DOTALL)
SOQL_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z0-9_]+)", re.IGNORECASE)
DYNAMIC_SOQL_RE = re.compile(r"Database\.(query|queryWithBinds|getQueryLocator|countQuery)\s*\(", re.IGNORECASE)
DML_RE = re.compile(r"\b(insert|update|delete|upsert|undelete|merge)\s+([A-Za-z_][\w\.\[\]]*)", re.IGNORECASE)
DML_DB_RE = re.compile(r"Database\.(insert|update|delete|upsert|undelete|merge)\s*\(", re.IGNORECASE)
CALLOUT_RE = re.compile(
    r"new\s+HttpRequest\s*\(|\bHttp\s*\(\)\s*\.\s*send|WebServiceCallout\.invoke|"
    r"@future\s*\(\s*callout\s*=\s*true", re.IGNORECASE)
NAMED_CRED_RE = re.compile(r"callout:([A-Za-z0-9_/]+)")
THROW_RE = re.compile(r"throw\s+new\s+([\w\.]+)")
CATCH_RE = re.compile(r"catch\s*\(\s*([\w\.]+)\s+(\w+)\s*\)")
CUSTOM_EXC_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+Exception\b")
TRIGGER_DECL_RE = re.compile(r"trigger\s+(\w+)\s+on\s+([\w.]+)\s*\(([^)]*)\)", re.IGNORECASE)
CALL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.\w+\s*\(")
METHOD_CALL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\.(\w+)\s*\(")
BATCHABLE_RE = re.compile(r"implements[^\{]*Database\.Batchable", re.IGNORECASE)

STATIC_COLLECTION_RE = re.compile(
    r"\b(?:public|private|global|protected)?\s*static\s+(final\s+)?(Map|List|Set)\s*<.*?>\s*(\w+)\s*=",
    re.IGNORECASE)
FIELD_WRITE_RE = re.compile(r"\b(\w+)\.([A-Za-z]\w*__c)\s*(?<![!<>=])=(?!=)\s*([^;]+);")
MAP_LOOKUP_RE = re.compile(r"^\s*(\w+)\s*\.\s*get\s*\(")
CONSTRUCTOR_RHS_RE = re.compile(r"^\s*new\s+")

CONTROL_KEYWORDS = {"if", "for", "while", "switch", "catch", "else", "try", "do",
                    "return", "new", "synchronized", "finally"}
SYSTEM_CLASSES = {"System", "Database", "Schema", "Test", "JSON", "Http", "Limits",
                  "UserInfo", "Datetime", "Date", "Math", "String", "Integer", "Decimal",
                  "EventBus", "Messaging", "ApexPages", "Trigger", "Type", "Blob"}
# Methods that belong to collections / primitives, not classes -- a receiver
# whose only calls are these is a variable, not a call target (P-03).
COLLECTION_PRIMITIVE_METHODS = {
    "get", "put", "add", "addall", "remove", "removeall", "size", "isempty", "clear",
    "contains", "containskey", "keyset", "values", "putall", "sort", "clone",
    "split", "substring", "tolowercase", "touppercase", "trim", "replace", "replaceall",
    "startswith", "endswith", "indexof", "length", "format", "valueof", "abbreviate",
    "equals", "hashcode", "tostring", "charat", "join", "left", "right", "deepclone",
}


def _line_of(code, pos):
    return code.count("\n", 0, pos) + 1


def _brace_span(code, open_idx):
    """Given the index of a '{', return the index just past its matching '}'."""
    depth = 0
    i = open_idx
    n = len(code)
    while i < n:
        c = code[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _loop_spans(code):
    """Char spans of for/while loop bodies, for in_loop detection."""
    spans = []
    for m in re.finditer(r"\b(for|while)\s*\(", code):
        brace = code.find("{", m.end())
        if brace != -1 and brace - m.end() < 400:
            spans.append((m.start(), _brace_span(code, brace)))
    return spans


def _in_any_span(pos, spans):
    return any(a <= pos < b for a, b in spans)


# ---------- method inventory (§6.4) ----------

METHOD_DECL_RE = re.compile(
    r"(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:(public|private|protected|global)\s+)?"
    r"(static\s+)?(?:override\s+|virtual\s+|abstract\s+|testmethod\s+)*"
    r"([\w<>\[\],.\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*\{",
    re.IGNORECASE)


def _annotations_before(code, start):
    window = code[max(0, start - 300):start]
    return sorted(set(re.findall(r"@(\w+)", window)))


def find_methods(code, class_name):
    methods = []
    for m in METHOD_DECL_RE.finditer(code):
        name = m.group(4)
        ret = (m.group(3) or "").strip().split()[-1] if (m.group(3) or "").strip() else None
        if name.lower() in CONTROL_KEYWORDS:
            continue
        # skip obvious non-methods (return type that is itself a control keyword)
        if ret and ret.lower() in CONTROL_KEYWORDS:
            continue
        open_brace = code.index("{", m.end() - 1)
        end = _brace_span(code, open_brace)
        body = code[open_brace:end]
        # Annotations sit inside the matched declaration (the regex consumes the
        # @Annotation prefix), so read them from the signature span itself.
        annotations = sorted(set(re.findall(r"@(\w+)", code[m.start():open_brace])))
        is_ctor = (name == class_name)
        methods.append({
            "name": name,
            "signature": f"{name}({m.group(5).strip()})",
            "visibility": (m.group(1) or "").lower() or None,
            "is_static": bool(m.group(2)),
            "returns": None if is_ctor else ret,
            "annotations": annotations,
            "loc": body.count("\n"),
            "cyclomatic_complexity": _cyclomatic(body),
            "_span": (m.start(), end),
        })
    return methods


def _cyclomatic(body):
    n = 1
    n += len(re.findall(r"\b(if|for|while|case|catch)\b", body))
    n += body.count("&&") + body.count("||") + body.count("?")
    return n


def _method_at(pos, methods):
    for m in methods:
        a, b = m["_span"]
        if a <= pos < b:
            return m["name"]
    return None


# ---------- exception handler characterisation (§6.1) ----------

def characterise_catches(code, methods):
    out = []
    for m in CATCH_RE.finditer(code):
        exc_type, var = m.group(1), m.group(2)
        brace = code.find("{", m.end())
        if brace == -1:
            continue
        end = _brace_span(code, brace)
        body = code[brace + 1:end - 1]
        stmts = [s for s in body.split(";") if s.strip()]
        handler = {
            "rethrows": bool(re.search(r"\bthrow\b\s+" + re.escape(var) + r"\b", body)),
            "wraps_and_rethrows": bool(re.search(r"\bthrow\s+new\b", body)),
            "adds_error": bool(re.search(r"\.addError\s*\(", body)),
            "persists_log": bool(re.search(r"\b(insert|upsert|update)\b|Database\.(insert|upsert)", body, re.IGNORECASE)),
            "publishes_event": bool(re.search(r"EventBus\.publish\s*\(", body)),
            "sends_email": bool(re.search(r"Messaging\.sendEmail\s*\(", body)),
            "debug_only": bool(re.search(r"System\.debug\s*\(", body)),
            "body_statement_count": len(stmts),
        }
        if handler["wraps_and_rethrows"]:
            effect = "wrap_rethrow"
        elif handler["rethrows"]:
            effect = "rethrow"
        elif handler["adds_error"]:
            effect = "add_error"
        elif handler["publishes_event"]:
            effect = "publishes_event"
        elif handler["persists_log"]:
            effect = "persist_log"
        elif handler["debug_only"] and handler["body_statement_count"] > 0:
            effect = "debug_only"
        elif handler["body_statement_count"] == 0:
            effect = "empty"
        else:
            effect = "swallow"
        handler["effect"] = effect
        handler["detail"] = _handler_detail(effect)
        out.append({"type": exc_type, "method": _method_at(m.start(), methods),
                    "line": _line_of(code, m.start()), "handler": handler})
    return out


def _handler_detail(effect):
    return {
        "empty": "catch block has no statements -- total silence",
        "swallow": "statements present but none surface the error",
        "debug_only": "System.debug only; visible only with debug logs on",
        "persist_log": "writes a log record; recoverable after the fact",
        "publishes_event": "publishes a platform event / notification",
        "add_error": "addError() surfaces the error to the user",
        "rethrow": "rethrows the caught exception",
        "wrap_rethrow": "wraps in a custom exception and rethrows",
    }.get(effect, effect)


# ---------- structured SOQL (§6.3) ----------

def parse_soql(snippet):
    inner = snippet.strip()
    if inner.startswith("["):
        inner = inner[1:-1].strip()
    from_m = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", inner, re.IGNORECASE)
    sel_m = re.search(r"\bSELECT\b(.*?)\bFROM\b", inner, re.IGNORECASE | re.DOTALL)
    fields = []
    subqueries = []
    if sel_m:
        raw_fields = sel_m.group(1)
        # pull out subqueries (parenthesised SELECTs)
        for sub in re.finditer(r"\(\s*SELECT\b.*?\bFROM\s+([A-Za-z0-9_]+).*?\)", raw_fields, re.IGNORECASE | re.DOTALL):
            subqueries.append(sub.group(1))
        raw_fields = re.sub(r"\(\s*SELECT\b.*?\)", "", raw_fields, flags=re.IGNORECASE | re.DOTALL)
        fields = [f.strip() for f in raw_fields.split(",") if f.strip()]
    where_m = re.search(r"\bWHERE\b(.*?)(\bORDER\s+BY\b|\bLIMIT\b|\bGROUP\s+BY\b|$)", inner, re.IGNORECASE | re.DOTALL)
    where_raw = where_m.group(1).strip() if where_m else None
    order_by = []
    for om in re.finditer(r"\bORDER\s+BY\s+(.*?)(\bLIMIT\b|$)", inner, re.IGNORECASE | re.DOTALL):
        for part in om.group(1).split(","):
            toks = part.strip().split()
            if toks:
                order_by.append({"field": toks[0],
                                 "direction": (toks[1].lower() if len(toks) > 1 else "asc")})
    limit_m = re.search(r"\bLIMIT\s+(\d+)", inner, re.IGNORECASE)
    return {
        "object": from_m.group(1) if from_m else None,
        "fields": fields,
        "where_raw": truncate(where_raw, 200) if where_raw else None,
        "order_by": order_by,
        "limit": int(limit_m.group(1)) if limit_m else None,
        "enforces_fls": bool(re.search(r"WITH\s+SECURITY_ENFORCED|WITH\s+USER_MODE", inner, re.IGNORECASE)),
        "relationship_subqueries": subqueries,
    }


# ---------- static state / field writes (kept, with v3 linkage) ----------

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


def _rhs_value(rhs):
    r = rhs.strip()
    low = r.lower()
    if low in ("true", "false"):
        return schema.value_literal("boolean", low == "true")
    if low == "null":
        return schema.value_literal("null", None, is_blank_assignment=True)
    if re.fullmatch(r"-?\d+(\.\d+)?", r):
        return schema.value_literal("number", float(r) if "." in r else int(r))
    if (r.startswith("'") and r.endswith("'")):
        return schema.value_literal("string", r[1:-1])
    return schema.value_reference(truncate(r, 120))


def find_field_writes(code, static_mutable_names, methods, dml_entries):
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
                    reason = (f"value comes from '{source}.get(...)', a static collection in this "
                              f"class that is never cleared or reset -- a stale or cross-record "
                              f"value can leak into this field with no exception.")
                else:
                    risk = "medium"
                    reason = (f"value comes from '{source}.get(...)', a static collection in this "
                              f"class -- it does appear to be reset somewhere, but verify the reset "
                              f"covers this code path.")
            else:
                risk = "medium"
                reason = f"value is borrowed from '{source}.get(...)' rather than computed directly for this record."

        method = _method_at(m.start(), methods)
        # persistence: is this receiver committed by a DML in the same method?
        persisted_by, persistence, passed_to = None, "unpersisted_or_unresolved", None
        for d in dml_entries:
            if receiver in d.get("receivers", []) and d.get("method") == method:
                persisted_by, persistence = d.get("line"), "persisted"
                break
        if persistence != "persisted":
            # receiver handed to another method? name the callee (Q-05) so the
            # commit can be traced across the boundary in the index pass.
            mcall = re.search(r"\b([A-Za-z_][\w.]*)\s*\([^)]*\b" + re.escape(receiver) + r"\b", code)
            if mcall and mcall.group(1) not in ("if", "for", "while", "return", "System"):
                persistence, passed_to = "passed_to_callee", mcall.group(1)

        writes.append({
            "field": field, "object": None, "receiver": receiver, "pattern": pattern,
            "source_map": source, "method": method, "line": _line_of(code, m.start()),
            "rhs": schema.value_literal("string", truncate(rhs, 120)) if pattern != "direct" else _rhs_value(rhs),
            "persisted_by": persisted_by, "passed_to": passed_to, "persistence": persistence,
            "risk": risk, "risk_derived_by": "static_analysis_" + schema.EXTRACTOR_VERSION,
            "reason": reason,
        })
    return writes


# ---------- DML (§6.2) ----------

def find_dml(code, methods, loop_spans):
    entries = []
    for m in DML_RE.finditer(code):
        op, target = m.group(1).lower(), m.group(2)
        base = target.split("[")[0].split(".")[0]
        entries.append({
            "op": op, "objects": [], "receivers": [base],
            "method": _method_at(m.start(), methods), "line": _line_of(code, m.start()),
            "in_loop": _in_any_span(m.start(), loop_spans),
            "is_bulk": _looks_bulk(target),
        })
    for m in DML_DB_RE.finditer(code):
        op = m.group(1).lower()
        entries.append({
            "op": op, "objects": [], "receivers": [], "method": _method_at(m.start(), methods),
            "line": _line_of(code, m.start()), "in_loop": _in_any_span(m.start(), loop_spans),
            "is_bulk": True, "via": "Database." + op,
        })
    return entries


def _looks_bulk(target):
    # a List/collection receiver is bulk-safe; a single sObject var is not a strong signal
    return target.strip().endswith("]") or target.strip().lower().endswith("list") or "list" in target.lower()


# ---------- calls_to classification (§6.5) ----------

def classify_calls(code, name, all_class_names):
    calls = {}
    for m in METHOD_CALL_RE.finditer(code):
        target, method = m.group(1), m.group(2)
        if target == name:
            continue
        # A class receiver is Capitalised or a known class; a lowercase receiver
        # that isn't a known class is a local variable (e.g. `e.getMessage()`),
        # not a call edge -- skip it to keep the call graph clean.
        if not (target[:1].isupper() or target in all_class_names):
            continue
        entry = calls.setdefault(target, {"target": target, "kind": None, "methods_called": set()})
        entry["methods_called"].add(method)
    out = []
    for target, e in calls.items():
        methods = e["methods_called"]
        if target in all_class_names:
            kind = "local_class"
        elif target in SYSTEM_CLASSES:
            continue  # system calls are noise for the call graph
        elif target.endswith("__c") or target.endswith("__r"):
            continue  # a field/relationship receiver, not a class (P-03)
        elif all(m.lower() in COLLECTION_PRIMITIVE_METHODS for m in methods):
            continue  # a collection/string variable, not a class (P-03)
        else:
            # unresolved receiver: could be a managed-package class or a local
            # variable we couldn't resolve to a type. Kept, but marked so a
            # genuine missing-class case is distinct from a resolved local one.
            kind = "unresolved_receiver"
        out.append({"target": target, "kind": kind, "methods_called": sorted(methods)})
    return out


# ---------- top-level ----------

def parse_class(name, raw_code, known_objects, all_class_names, namespace_prefix=None, api_version=None):
    code = strip_comments(raw_code)
    methods = find_methods(code, name)
    loop_spans = _loop_spans(code)

    card = {
        "id": name, "type": "ApexClass",
        **schema.envelope(api_version),
        **schema.namespace_fields(namespace_prefix, name),
        "loc": raw_code.count("\n") + 1, "sharing": "not_specified",
        "is_test_class": False, "extends": None, "implements": [], "entry_points": [],
        "methods": [], "soql": [], "dml": [], "callouts": [], "named_credentials": [],
        "exceptions_thrown": [], "exceptions_caught": [], "custom_exceptions_defined": [],
        "calls_to": [], "objects_referenced": [], "static_mutable_state": [], "field_writes": [],
    }

    decl = CLASS_DECL_RE.search(code)
    if decl:
        card["sharing"] = decl.group(3).lower() if decl.group(3) else "inherited"
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

    # SOQL (structured)
    for m in SOQL_RE.finditer(code):
        s = parse_soql(m.group(0))
        s["method"] = _method_at(m.start(), methods)
        s["line"] = _line_of(code, m.start())
        s["in_loop"] = _in_any_span(m.start(), loop_spans)
        s["is_dynamic"] = False
        card["soql"].append(s)
    if DYNAMIC_SOQL_RE.search(code):
        for m in DYNAMIC_SOQL_RE.finditer(code):
            card["soql"].append({"object": None, "is_dynamic": True, "fields": [], "where_raw": None,
                                 "order_by": [], "limit": None, "method": _method_at(m.start(), methods),
                                 "line": _line_of(code, m.start()), "in_loop": _in_any_span(m.start(), loop_spans),
                                 "unresolved_fragments": ["dynamic query string not statically resolvable"]})

    card["dml"] = find_dml(code, methods, loop_spans)

    if CALLOUT_RE.search(code):
        card["callouts"].append({"mechanism": "HTTP callout (HttpRequest/@future callout)"})
    for m in NAMED_CRED_RE.finditer(code):
        card["named_credentials"].append(m.group(1))

    card["exceptions_thrown"] = sorted(set(THROW_RE.findall(code)))
    card["exceptions_caught"] = characterise_catches(code, methods)
    card["custom_exceptions_defined"] = sorted(set(CUSTOM_EXC_RE.findall(code)))

    card["calls_to"] = classify_calls(code, name, all_class_names)
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names, methods, card["dml"])

    card["methods"] = [{k: v for k, v in m.items() if k != "_span"} for m in methods]
    return card


def parse_trigger(name, raw_code, known_objects, all_class_names, namespace_prefix=None, api_version=None):
    code = strip_comments(raw_code)
    methods = []  # triggers have no methods; DML/writes attribute to None
    loop_spans = _loop_spans(code)
    card = {
        "id": name, "type": "ApexTrigger",
        **schema.envelope(api_version),
        **schema.namespace_fields(namespace_prefix, name),
        "loc": raw_code.count("\n") + 1, "object": None, "events": [], "entry_points": [],
        "soql": [], "dml": [], "callouts": [], "exceptions_thrown": [], "exceptions_caught": [],
        "calls_to": [], "objects_referenced": [], "static_mutable_state": [], "field_writes": [],
    }
    m = TRIGGER_DECL_RE.search(code)
    if m:
        card["object"] = m.group(2)
        card["events"] = [e.strip() for e in m.group(3).split(",")]

    for m2 in SOQL_RE.finditer(code):
        s = parse_soql(m2.group(0))
        s["line"] = _line_of(code, m2.start())
        s["in_loop"] = _in_any_span(m2.start(), loop_spans)
        s["is_dynamic"] = False
        card["soql"].append(s)

    card["dml"] = find_dml(code, methods, loop_spans)

    if CALLOUT_RE.search(code):
        card["callouts"].append({"mechanism": "HTTP callout"})

    card["exceptions_thrown"] = sorted(set(THROW_RE.findall(code)))
    card["exceptions_caught"] = characterise_catches(code, methods)
    card["calls_to"] = classify_calls(code, name, all_class_names)
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names, methods, card["dml"])

    if card["object"]:
        card["entry_points"].append({"kind": "Trigger", "detail": f"{card['object']} ({', '.join(card['events'])})"})
    return card
