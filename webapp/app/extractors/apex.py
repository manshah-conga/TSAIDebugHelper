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
# `<>` is allowed so generic interfaces parse: `implements Database.Batchable<Id>,
# Database.Stateful {` used to fail this match outright and leave implements = [].
IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w\.,\s<>]+?)\s*\{", re.IGNORECASE)
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
# Queueable / Schedulable may appear anywhere in the implements list
# (`implements Database.AllowsCallouts, Queueable`), not only first.
QUEUEABLE_RE = re.compile(r"implements[^\{]*\bQueueable\b", re.IGNORECASE)
SCHEDULABLE_RE = re.compile(r"implements[^\{]*\bSchedulable\b", re.IGNORECASE)

# Instantiation edges. `new X(` of a class in this org is a real dependency
# even when no `X.method(` call follows -- the common case being a job handed
# straight to System.enqueueJob / Database.executeBatch.
NEW_INSTANCE_RE = re.compile(r"\bnew\s+([A-Za-z_]\w*)(?:\s*\.\s*([A-Za-z_]\w*))?\s*\(")
TYPE_FORNAME_RE = re.compile(
    r"\bType\s*\.\s*forName\s*\(\s*(?:'([\w]*)'\s*,\s*)?'([\w.]+)'\s*\)", re.IGNORECASE)

# Async dispatch: which argument (0-based) is the job instance, and which
# optional argument carries a delay / scope size worth surfacing.
ASYNC_DISPATCH = [
    ("System.enqueueJob",    re.compile(r"\bSystem\s*\.\s*enqueueJob\s*\(", re.IGNORECASE),
     0, {"delay_minutes": 1}),
    ("Database.executeBatch", re.compile(r"\bDatabase\s*\.\s*executeBatch\s*\(", re.IGNORECASE),
     0, {"scope_size": 1}),
    ("System.scheduleBatch", re.compile(r"\bSystem\s*\.\s*scheduleBatch\s*\(", re.IGNORECASE),
     0, {"job_name": 1, "delay_minutes": 2, "scope_size": 3}),
    ("System.schedule",      re.compile(r"\bSystem\s*\.\s*schedule\s*\(", re.IGNORECASE),
     2, {"job_name": 0, "cron": 1}),
]
# Types that say "some job" without saying which -- a variable declared as
# one of these cannot be resolved to a class from its declaration alone.
_ASYNC_INTERFACE_TYPES = {"queueable", "schedulable", "database.batchable", "object",
                          "system.queueable", "system.schedulable"}

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

# The return-type group is a run of whitespace-free tokens joined by `\s+`
# (`Map<String, List<Id>>` -> `Map<String,` + `List<Id>>`). It must NOT
# contain `\s` itself: the old `[\w<>\[\],.\s]+?` followed by `\s+` could split
# any whitespace run between the two quantifiers in O(n^2) ways, at every one
# of O(n) start positions. A class whose tail is a big block of `//`
# comments (stripped to bare newlines, no `(`) then took cubic time -- 400
# lines ~24s, a few thousand lines effectively forever -- and hung the fetch
# with no error (ContactTriggerUtilityTest).
# A declaration can only begin at the start of the source or right after
# `{`, `}` or `;` (plus whitespace), so the match is anchored there. Without
# the anchor every token of a long `(`-free run (a 500-field SOQL select
# list) is a start position that rescans the rest of the run -- quadratic.
METHOD_DECL_RE = re.compile(
    r"(?:(?<=[{};])|^)\s*"
    r"(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:(public|private|protected|global)\s+)?"
    r"(static\s+)?(?:override\s+|virtual\s+|abstract\s+|testmethod\s+)*"
    r"([\w<>\[\],.]+(?:\s+[\w<>\[\],.]+)*?)\s+(\w+)\s*\(([^)]*)\)\s*\{",
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
        # The lazy return-type group can start on the whitespace BEFORE the
        # modifiers and swallow them ("\n public static void"), leaving
        # group(1)/(2) empty. Recover the modifiers from that text.
        ret_words = [w.lower() for w in (m.group(3) or "").split()]
        visibility = (m.group(1) or "").lower() or next(
            (w for w in ret_words if w in ("public", "private", "protected", "global")), None)
        is_static = bool(m.group(2)) or "static" in ret_words
        methods.append({
            "name": name,
            "signature": f"{name}({m.group(5).strip()})",
            "visibility": visibility,
            "is_static": is_static,
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


# ---------- async dispatch (enqueueJob / executeBatch / schedule) ----------

def _call_args(code, open_paren):
    """Top-level, comma-separated argument texts of the call whose '(' is at
    `open_paren`. String-literal aware, so a ',' or ')' inside '...' does not
    split or close the call. Returns (args, index_past_close)."""
    depth, i, n = 0, open_paren, len(code)
    start, args, in_str = open_paren + 1, [], False
    while i < n:
        c = code[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                in_str = False
        elif c == "'":
            in_str = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                tail = code[start:i].strip()
                if tail or args:
                    args.append(tail)
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(code[start:i].strip())
            start = i + 1
        i += 1
    return args, n


def _method_body(pos, methods, code):
    for m in methods:
        a, b = m["_span"]
        if a <= pos < b:
            return code[a:b]
    return code


def _resolve_identifier_type(var, scope, code):
    """What class does variable `var` hold? Looks for `var = new X(` first
    (the concrete class, even when the declared type is an interface), then a
    declaration `X var` whose type is not a generic job interface. Searches
    the enclosing method, then the whole class (member fields)."""
    for text in (scope, code):
        m = re.search(r"\b" + re.escape(var) + r"\s*=\s*new\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(", text)
        if m:
            return m.group(1), "variable_assignment"
    for text in (scope, code):
        for m in re.finditer(r"\b([A-Za-z_][\w.]*)\s+" + re.escape(var) + r"\s*[=;,)]", text):
            t = m.group(1)
            if t.lower() in _ASYNC_INTERFACE_TYPES or t.lower() in CONTROL_KEYWORDS:
                continue
            if t.lower() in ("final", "static", "public", "private", "protected", "global", "transient"):
                continue
            return t, "declared_type"
    return None, "unresolved"


def _resolve_job_expr(expr, scope, code, class_name):
    e = (expr or "").strip()
    # strip a cast: (Queueable) new X()
    e = re.sub(r"^\(\s*[\w.<>]+\s*\)\s*", "", e)
    m = NEW_INSTANCE_RE.match(e)
    if m:
        return (f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)), "constructor"
    if e == "this":
        return class_name, "this"
    ident = re.fullmatch(r"(?:this\s*\.\s*)?([A-Za-z_]\w*)", e)
    if ident:
        return _resolve_identifier_type(ident.group(1), scope, code)
    m = NEW_INSTANCE_RE.search(e)   # ternary / wrapped expression
    if m:
        return (f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)), "constructor_in_expression"
    return None, "unresolved"


def _resolve_literal(arg, scope, code):
    """An int literal, or an identifier assigned an int literal in scope
    (`Integer delayInMinutes = 2;`). Anything else is returned as text."""
    a = (arg or "").strip()
    if re.fullmatch(r"\d+", a):
        return int(a)
    if re.fullmatch(r"'[^']*'", a):
        return a[1:-1]
    if re.fullmatch(r"[A-Za-z_]\w*", a):
        for text in (scope, code):
            m = re.search(r"\b" + re.escape(a) + r"\s*=\s*(\d+)\s*;", text)
            if m:
                return int(m.group(1))
    return truncate(a, 80) if a else None


def find_async_dispatches(code, class_name, methods, loop_spans, all_class_names):
    """Every place this component starts asynchronous Apex:
    System.enqueueJob / Database.executeBatch / System.scheduleBatch /
    System.schedule, with the job class resolved where the source allows.

    Without this, the only edge to a Queueable was a `X.method(` call, which
    enqueue code never makes -- `System.enqueueJob(new X(ids), 2)` produced no
    edge at all, and 'who enqueues X?' came back empty."""
    out = []
    for mechanism, rx, job_idx, extras in ASYNC_DISPATCH:
        for m in rx.finditer(code):
            args, _end = _call_args(code, m.end() - 1)
            scope = _method_body(m.start(), methods, code)
            job_expr = args[job_idx] if len(args) > job_idx else None
            target, how = _resolve_job_expr(job_expr, scope, code, class_name)
            inner = None
            if target and "." in target:
                outer, inner = target.split(".", 1)
                target = outer if outer in all_class_names else target
            elif target and target not in all_class_names and target != class_name and \
                    re.search(r"\bclass\s+" + re.escape(target) + r"\b", code):
                # an inner class of this same file
                inner, target = target, class_name
            entry = {
                "mechanism": mechanism,
                "target": target,
                "inner_class": inner,
                "target_in_kb": bool(target) and (target in all_class_names or target == class_name),
                "resolution": how,
                "job_expression": truncate(job_expr, 120) if job_expr else None,
                "method": _method_at(m.start(), methods),
                "line": _line_of(code, m.start()),
                "in_loop": _in_any_span(m.start(), loop_spans),
            }
            for key, idx in extras.items():
                if len(args) > idx:
                    entry[key] = _resolve_literal(args[idx], scope, code)
            out.append(entry)
    return out


# ---------- calls_to classification (§6.5) ----------

def classify_calls(code, name, all_class_names, async_dispatches=None):
    calls = {}

    def _edge(target, via):
        e = calls.setdefault(target, {"target": target, "kind": None, "methods_called": set(), "via": set()})
        e["via"].add(via)
        return e

    for m in METHOD_CALL_RE.finditer(code):
        target, method = m.group(1), m.group(2)
        if target == name:
            continue
        # A class receiver is Capitalised or a known class; a lowercase receiver
        # that isn't a known class is a local variable (e.g. `e.getMessage()`),
        # not a call edge -- skip it to keep the call graph clean.
        if not (target[:1].isupper() or target in all_class_names):
            continue
        _edge(target, "method_call")["methods_called"].add(method)

    # Instantiation of an org class: `new X(...)` / `new Outer.Inner(...)`.
    # Restricted to classes known to be in the org so `new Map<..>(` and
    # `new Account(` never become edges.
    for m in NEW_INSTANCE_RE.finditer(code):
        target = m.group(1)
        if target != name and target in all_class_names:
            _edge(target, "constructor")

    # Reflection with a literal class name.
    for m in TYPE_FORNAME_RE.finditer(code):
        parts = [m.group(2)] if m.group(1) else m.group(2).split(".")
        target = next((p for p in parts if p in all_class_names), None)
        if target and target != name:
            _edge(target, "Type.forName")

    for d in async_dispatches or []:
        if d.get("target") and d["target"] != name and d["target"] in all_class_names:
            _edge(d["target"], d["mechanism"])

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
        out.append({"target": target, "kind": kind, "methods_called": sorted(methods),
                    "via": sorted(e["via"])})
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

    # Job interfaces: judged on each class HEADER (`class X ... {`), so an inner
    # class implementing Queueable is reported as that inner class rather than
    # making the outer class look like a Queueable itself.
    for hm in re.finditer(r"\bclass\s+(\w+)([^{;]*)\{", code):
        cls = hm.group(1)
        im = re.search(r"\bimplements\b", hm.group(2), re.IGNORECASE)
        if not im:
            continue
        header = hm.group(2)[im.start():]
        suffix = "" if cls == name else f" (inner class {cls})"
        for kind, rx, detail in (("Batchable", BATCHABLE_RE, "Database.Batchable"),
                                 ("Queueable", QUEUEABLE_RE, "Queueable"),
                                 ("Schedulable", SCHEDULABLE_RE, "Schedulable")):
            if rx.search(header + "{"):
                entry = {"kind": kind, "detail": detail + suffix}
                if cls != name:
                    entry["inner_class"] = cls
                card["entry_points"].append(entry)
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

    card["async_dispatches"] = find_async_dispatches(code, name, methods, loop_spans, all_class_names)
    card["calls_to"] = classify_calls(code, name, all_class_names, card["async_dispatches"])
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names, methods, card["dml"])

    card["methods"] =[{k: v for k, v in m.items() if k != "_span"} for m in methods]
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
    card["async_dispatches"] = find_async_dispatches(code, name, methods, loop_spans, all_class_names)
    card["calls_to"] = classify_calls(code, name, all_class_names, card["async_dispatches"])
    card["objects_referenced"] = find_objects_referenced(code, known_objects)

    static_state = find_static_mutable_state(code)
    card["static_mutable_state"] = static_state
    static_names = {s["name"]: s["cleared_or_reassigned_elsewhere"] for s in static_state}
    card["field_writes"] = find_field_writes(code, static_names, methods, card["dml"])

    if card["object"]:
        card["entry_points"].append({"kind": "Trigger", "detail": f"{card['object']} ({', '.join(card['events'])})"})
    return card
