"""
Condenses a raw Salesforce Apex debug log (received as an in-memory
upload, never written to disk) into the same high-signal JSON shape the
standalone normalize_log.py produces. See that script's module docstring
for the full rationale of what's kept vs dropped.
"""
import re
from collections import defaultdict

DML_BEGIN_RE = re.compile(r"Op:(\w+)\|Type:(\w+)\|Rows:(\d+)")
SOQL_END_ROWS_RE = re.compile(r"Rows:(\d+)")
# "  Number of SOQL queries: 12 out of 100"
# "  Maximum CPU time: 9800 out of 10000 ******* CLOSE TO LIMIT"
LIMIT_LINE_RE = re.compile(r"^\s*(?:Number of|Maximum)\s+(.+?):\s*(\d+)\s*out of\s*(\d+)(.*)$")
# "21:20:56.1 (1234)|LIMIT_USAGE_FOR_NS|(default)|" -- one block per namespace
LIMIT_NS_RE = re.compile(r"\|LIMIT_USAGE_FOR_NS\|([^|]*)")
DEFAULT_NS = "(default)"
CLASS_METHOD_STACK_RE = re.compile(r"^Class\.([\w.]+?)(?:\.(\w+))?:\s*line\s*(\d+)")
TRIGGER_STACK_RE = re.compile(r"^Trigger\.(\w+):\s*line\s*(\d+)")
NORMALIZE_LITERAL_RE = re.compile(r"'[^']*'|:\w+|\b\d+\b")


def normalize_signature(text):
    return NORMALIZE_LITERAL_RE.sub("?", text).strip()


def truncate(s, n=180):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def parse_limit_blocks(lines):
    """Final governor-limit usage, per namespace.

    Salesforce writes a LIMIT_USAGE_FOR_NS|<ns>| header followed by
    "Number of X: N out of M" lines -- one block for the org's own code
    ("(default)") and one per managed package that ran (Apttus, ...). The
    blocks repeat at each CUMULATIVE_LIMIT_USAGE checkpoint, so the last block
    per namespace is the end-of-transaction figure. Merging all blocks into one
    dict (the old behaviour) let a managed package's all-zero block overwrite
    the org's real usage.

    Returns (limits_final, limits_by_namespace). limits_final is the default
    namespace (the customer's code), falling back to the first namespace seen.

    Checkpoints are cumulative within one transaction, so usage should never
    go down. When it does, the log holds more than one transaction (async
    chains, batch chunks, resumed flows) and the last block can be a tiny
    trailing one -- e.g. "0 CPU time" after 1,600+ code units. So each entry
    also carries `peak_used` (highest value seen at any checkpoint) whenever
    it exceeds the final figure.
    """
    by_ns, order, current = {}, [], None
    peaks = defaultdict(dict)
    for raw in lines:
        ns_m = LIMIT_NS_RE.search(raw)
        if ns_m:
            current = ns_m.group(1).strip() or DEFAULT_NS
            by_ns[current] = {}  # new checkpoint: replace, don't merge
            if current not in order:
                order.append(current)
            continue
        m = LIMIT_LINE_RE.match(raw)
        if not m:
            continue
        ns = current or DEFAULT_NS
        if ns not in by_ns:
            by_ns[ns] = {}
            order.append(ns)
        name, used = m.group(1).strip(), int(m.group(2))
        entry = {"used": used, "max": int(m.group(3))}
        if "CLOSE TO LIMIT" in m.group(4).upper():
            entry["close_to_limit"] = True
        by_ns[ns][name] = entry
        peaks[ns][name] = max(peaks[ns].get(name, 0), used)
    if not order:
        return {}, {}
    for ns in order:
        for name, entry in by_ns[ns].items():
            if peaks[ns].get(name, 0) > entry["used"]:
                entry["peak_used"] = peaks[ns][name]
    final = by_ns.get(DEFAULT_NS) or by_ns[order[0]]
    return final, {ns: by_ns[ns] for ns in order}


def parse_debug_level_header(raw_header):
    """The first line of a raw debug log is a verbatim log-level directive,
    e.g. '59.0 APEX_CODE,FINE;APEX_PROFILING,NONE'. We never persist that
    raw string (it is still 'the actual data', just a short one) -- instead
    we parse it into structured fields so the only thing that reaches disk
    is derived signal (which categories were logged, at what level)."""
    raw_header = (raw_header or "").strip()
    if not raw_header:
        return {"api_version": None, "log_levels": {}}
    parts = raw_header.split(None, 1)
    api_version = parts[0] if parts and re.match(r"^\d+(\.\d+)?$", parts[0]) else None
    flags = parts[1] if len(parts) > 1 else (parts[0] if api_version is None else "")
    levels = {}
    for chunk in flags.split(";"):
        chunk = chunk.strip()
        if "," in chunk:
            cat, lvl = chunk.split(",", 1)
            if cat.strip():
                levels[cat.strip()] = lvl.strip()
    return {"api_version": api_version, "log_levels": levels}


_NAME_PAT = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{2,})\b")


def involved_components(execution_units, exceptions, index_ids=None):
    """Component names the log mentions, from execution-unit labels and
    exception messages/stacks. With `index_ids` (an org's component ids) only
    names in that org count; without one, a prefix heuristic stands in.

    Kept separate from parse_log_text because everything it reads survives
    normalization: a log normalized with no org (the library) can be
    re-matched against an org's index later, when it is filed as an incident
    there, and get exactly what parsing the raw log against that org would
    have produced."""
    involved = set()
    text_blob = " ".join(
        [u.get("label") or "" for u in execution_units or []]
        + [e.get("message") or "" for e in exceptions or []]
        + [" ".join(e.get("stack") or []) for e in exceptions or []]
    )
    for m in _NAME_PAT.finditer(text_blob):
        tok = m.group(1)
        if index_ids and tok in index_ids:
            involved.add(tok)
        elif not index_ids and (tok.startswith("ibmc") or tok.startswith("itcc") or tok.startswith("APTS")):
            involved.add(tok)
    return sorted(involved)


# ---------- async job evidence ----------
#
# 2026-10-08: "Too many queueable jobs added to the queue: 2" in a Queueable
# (Apttus CreateOrderQJob) -- the RCA needs WHICH jobs were enqueued, and the
# normalizer dropped every event that could say so. With APEX_CODE >= FINE a
# log has METHOD_ENTRY / CONSTRUCTOR_ENTRY (who called whom, at which line);
# with SYSTEM >= FINE it also has SYSTEM_METHOD_ENTRY for System.enqueueJob
# itself. Together they name the job class and the enqueuing line.

ASYNC_SYSTEM_METHODS = (
    ("System.enqueueJob", "System.enqueueJob"),
    ("Database.executeBatch", "Database.executeBatch"),
    ("System.scheduleBatch", "System.scheduleBatch"),
    ("System.schedule", "System.schedule"),
)
JOB_NAME_RE = re.compile(r"queu|queable|job|batch|schedul|async|future|worker", re.I)
ASYNC_LIMIT_RE = re.compile(r"Too many (queueable jobs added to the queue|future calls|"
                            r"batch jobs|async)\S*:?\s*(\d+)?", re.I)
LINE_RE = re.compile(r"^\[(\d+)\]$")
MAX_JOB_CONSTRUCTIONS = 60
_LEVELS = ("NONE", "ERROR", "WARN", "INFO", "DEBUG", "FINE", "FINER", "FINEST")


def _line_no(field):
    m = LINE_RE.match((field or "").strip())
    return int(m.group(1)) if m else None


def _callee(sig):
    """'Ns.Class.method(List<X>, Id)' -> 'Ns.Class.method'."""
    return (sig or "").split("(", 1)[0].strip()


def _level_at_least(levels, cat, want):
    have = (levels.get(cat) or "NONE").upper()
    return have in _LEVELS and _LEVELS.index(have) >= _LEVELS.index(want)


class _AsyncTracker:
    """Method-frame stack per code unit, plus what async work each frame
    started. Fed one event at a time from parse_log_text."""

    def __init__(self):
        self.frames = []            # [{"unit": label, "methods": [name, ...], "last_ctor": {...}}]
        self.dispatches = []
        self.constructions = {}     # (class, line, caller) -> entry
        self.failed = []
        self.managed_entries = 0

    def caller(self):
        if not self.frames:
            return None
        f = self.frames[-1]
        return f["methods"][-1] if f["methods"] else f["unit"]

    def unit_started(self, label):
        self.frames.append({"unit": label, "methods": [], "last_ctor": None})

    def unit_finished(self):
        if self.frames:
            self.frames.pop()

    def method_entry(self, parts):
        if self.frames:
            self.frames[-1]["methods"].append(_callee(parts[-1]))

    def method_exit(self, parts):
        """Pop back to the matching entry. Logs are not perfectly paired
        (some exits have no entry), so an exit naming nothing on the stack
        is ignored rather than popping an unrelated frame."""
        if not self.frames:
            return
        stack, name = self.frames[-1]["methods"], _callee(parts[-1])
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] == name:
                del stack[i:]
                return

    def constructor(self, parts):
        cls = (parts[-1] or "").strip()
        line = _line_no(parts[2]) if len(parts) > 2 else None
        if not cls or cls.startswith("<"):
            return
        entry = {"class": cls, "line": line, "caller": self.caller(),
                 "code_unit": self.frames[-1]["unit"] if self.frames else None}
        if self.frames:
            self.frames[-1]["last_ctor"] = entry
        if JOB_NAME_RE.search(cls):
            key = (cls, line, entry["caller"])
            if key in self.constructions:
                self.constructions[key]["count"] += 1
            elif len(self.constructions) < MAX_JOB_CONSTRUCTIONS:
                self.constructions[key] = dict(entry, count=1)

    def _nearest_ctor(self, line):
        """The last object constructed in this frame -- `System.enqueueJob(new
        X())` constructs X on the same line; `X j = new X(); enqueueJob(j);` a
        line or two before. The line is reported so the reader can judge."""
        c = self.frames[-1]["last_ctor"] if self.frames else None
        if c and c.get("caller") == self.caller() and (line is None or c["line"] is None
                                                       or 0 <= line - c["line"] <= 25):
            return {"class": c["class"], "line": c["line"]}
        return None

    def system_method(self, parts):
        sig = parts[-1] if parts else ""
        for prefix, mech in ASYNC_SYSTEM_METHODS:
            if sig.startswith(prefix + "("):
                line = _line_no(parts[2]) if len(parts) > 2 else None
                self.dispatches.append({
                    "mechanism": mech, "line": line, "caller": self.caller(),
                    "code_unit": self.frames[-1]["unit"] if self.frames else None,
                    "job_class_guess": self._nearest_ctor(line), "failed": False,
                })
                return

    def exception(self, parts, msg):
        m = ASYNC_LIMIT_RE.search(msg or "")
        if not m:
            return
        line = _line_no(parts[2]) if len(parts) > 3 else None
        # The enqueue that tripped the limit is the last dispatch at this line.
        for d in reversed(self.dispatches):
            if d["line"] == line and d["caller"] == self.caller():
                d["failed"] = True
                break
        self.failed.append({
            "message": truncate(msg, 200), "line": line, "caller": self.caller(),
            "code_unit": self.frames[-1]["unit"] if self.frames else None,
            "attempted_count": int(m.group(2)) if m.group(2) else None,
            "job_class_guess": self._nearest_ctor(line),
        })

    def result(self, header, execution_units):
        levels = header.get("log_levels") or {}
        visibility = []
        if not _level_at_least(levels, "APEX_CODE", "FINE"):
            visibility.append("APEX_CODE is below FINE: method and constructor entries are not "
                              "logged, so job classes and callers cannot be seen.")
        if not _level_at_least(levels, "SYSTEM", "FINE"):
            visibility.append("SYSTEM is below FINE: System.enqueueJob / Database.executeBatch "
                              "calls themselves are not logged. job_constructions (classes that "
                              "look like jobs, constructed with caller and line) are the best "
                              "evidence; re-capture with SYSTEM=FINE to see every enqueue.")
        if self.managed_entries:
            visibility.append(f"Managed-package code ran {self.managed_entries} time(s) "
                              f"(ENTERING_MANAGED_PKG); its internals, including any enqueues, "
                              f"are hidden from the log.")
        top = next((u["label"] for u in execution_units if u["depth"] == 0), None)
        context = None
        if top:
            looks_async = bool(JOB_NAME_RE.search(top)) and "__sfdc_trigger" not in top
            context = {
                "entry_unit": top,
                "likely_async": looks_async,
                "note": ("Entry unit looks like an async job (Queueable/Batch/Future). In an "
                         "async transaction only ONE System.enqueueJob is allowed, so a second "
                         "enqueue anywhere in this transaction -- including from triggers it "
                         "fires -- throws 'Too many queueable jobs added to the queue: 2'. "
                         "In a synchronous transaction the limit is 50.") if looks_async else None,
            }
        # Visibility notes alone are noise (SYSTEM=DEBUG is the default), so
        # the section exists only when something async actually showed up.
        if not (self.dispatches or self.constructions or self.failed
                or (context and context["likely_async"])):
            return None
        return {
            "context": context,
            "dispatches": self.dispatches[:100],
            "failed_async_limit": self.failed,
            "job_constructions": list(self.constructions.values()),
            "visibility": visibility,
        }


def parse_log_text(text, index_ids=None):
    lines = text.splitlines()
    header = parse_debug_level_header(lines[0] if lines else "")
    tracker = _AsyncTracker()

    code_unit_stack, execution_units = [], []
    soql_open, dml_open = [], []
    soql_events, dml_events = [], []
    callouts, user_debug, exceptions, flow_events, validation_failures = [], [], [], [], []
    in_fatal_stack = False
    current_stack_lines, current_exception = [], None

    for raw in lines[1:]:
        if not raw.strip():
            continue
        parts = raw.split("|")

        if in_fatal_stack:
            stripped = raw.strip()
            if CLASS_METHOD_STACK_RE.match(stripped) or TRIGGER_STACK_RE.match(stripped):
                current_stack_lines.append(stripped)
                continue
            else:
                in_fatal_stack = False
                if current_exception is not None:
                    current_exception["stack"] = current_stack_lines
                    current_stack_lines, current_exception = [], None

        if len(parts) < 2:
            continue
        evt = parts[1]

        if evt == "CODE_UNIT_STARTED":
            label = parts[-1] if len(parts) >= 3 else ""
            frame = {"label": label, "depth": len(code_unit_stack), "had_exception": False}
            execution_units.append(frame)
            code_unit_stack.append(frame)
            tracker.unit_started(label)
        elif evt == "CODE_UNIT_FINISHED":
            if code_unit_stack:
                code_unit_stack.pop()
            tracker.unit_finished()
        elif evt == "METHOD_ENTRY":
            tracker.method_entry(parts)
        elif evt == "METHOD_EXIT":
            tracker.method_exit(parts)
        elif evt == "CONSTRUCTOR_ENTRY":
            tracker.constructor(parts)
        elif evt == "SYSTEM_METHOD_ENTRY":
            tracker.system_method(parts)
        elif evt == "ENTERING_MANAGED_PKG":
            tracker.managed_entries += 1
        elif evt == "SOQL_EXECUTE_BEGIN":
            query = parts[-1] if len(parts) >= 4 else ""
            soql_open.append({"query": query})
        elif evt == "SOQL_EXECUTE_END":
            rows_m = SOQL_END_ROWS_RE.search(parts[-1]) if len(parts) >= 3 else None
            rows = int(rows_m.group(1)) if rows_m else None
            opened = soql_open.pop() if soql_open else {"query": ""}
            from_m = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", opened["query"], re.IGNORECASE)
            soql_events.append({
                "signature": normalize_signature(opened["query"]),
                "object": from_m.group(1) if from_m else None, "rows": rows,
                "raw_example": truncate(opened["query"], 150),
            })
        elif evt == "DML_BEGIN":
            detail = "|".join(parts[2:]) if len(parts) >= 3 else ""
            dml_open.append({"detail": detail})
        elif evt == "DML_END":
            opened = dml_open.pop() if dml_open else {"detail": ""}
            m = DML_BEGIN_RE.search(opened["detail"])
            if m:
                dml_events.append({"operation": m.group(1), "object": m.group(2), "rows": int(m.group(3))})
            else:
                dml_events.append({"operation": "Unknown", "object": None, "rows": None, "raw": opened["detail"]})
        elif evt in ("CALLOUT_REQUEST", "CALLOUT_RESPONSE", "NAMED_CREDENTIAL_REQUEST", "NAMED_CREDENTIAL_RESPONSE"):
            callouts.append({"event": evt, "detail": truncate(parts[-1], 200) if len(parts) >= 3 else ""})
        elif evt == "USER_DEBUG":
            user_debug.append(truncate(parts[-1], 300) if len(parts) >= 4 else raw)
        elif evt in ("VALIDATION_RULE", "VALIDATION_FAIL", "VALIDATION_FORMULA"):
            if "FAIL" in evt or evt == "VALIDATION_RULE":
                validation_failures.append(truncate(raw, 200))
        elif evt.startswith("FLOW_"):
            if evt in ("FLOW_START_INTERVIEW_BEGIN", "FLOW_START_INTERVIEW_END", "FLOW_ELEMENT_ERROR", "FLOW_FAULT_EVENT", "FLOW_INTERVIEW_FINISHED"):
                flow_events.append({"event": evt, "detail": truncate(parts[-1], 200) if len(parts) >= 3 else ""})
        elif evt == "EXCEPTION_THROWN":
            msg = parts[-1] if len(parts) >= 3 else ""
            current_exception = {"type": msg.split(":")[0].strip(), "message": msg, "stack": []}
            exceptions.append(current_exception)
            for frame in code_unit_stack:
                frame["had_exception"] = True
            tracker.exception(parts, msg)
        elif evt == "FATAL_ERROR":
            in_fatal_stack = True
            current_stack_lines = []
            if current_exception is None:
                msg = parts[-1] if len(parts) >= 3 else raw
                current_exception = {"type": msg.split(":")[0].strip(), "message": msg, "stack": []}
                exceptions.append(current_exception)

    limits_final, limits_by_namespace = parse_limit_blocks(lines)

    def collapse(events, group_key):
        groups = defaultdict(lambda: {"count": 0, "total_rows": 0, "example": None})
        for e in events:
            g = groups[e.get(group_key)]
            g["count"] += 1
            g["total_rows"] += e.get("rows") or 0
            if g["example"] is None:
                g["example"] = e
        out = []
        for g in groups.values():
            item = dict(g["example"])
            item["occurrences"], item["total_rows"] = g["count"], g["total_rows"]
            out.append(item)
        return out

    exc_groups, exc_order = {}, []
    for e in exceptions:
        key = (e["type"], e["message"], tuple(e.get("stack", [])))
        if key not in exc_groups:
            exc_groups[key] = dict(e)
            exc_groups[key]["occurrences"] = 0
            exc_order.append(key)
        exc_groups[key]["occurrences"] += 1
    exceptions = [exc_groups[k] for k in exc_order]

    soql_summary = collapse(soql_events, "signature")
    dml_summary_map = defaultdict(lambda: {"count": 0, "total_rows": 0})
    for e in dml_events:
        k = (e.get("operation"), e.get("object"))
        dml_summary_map[k]["count"] += 1
        dml_summary_map[k]["total_rows"] += e.get("rows") or 0
    dml_summary = [
        {"operation": op, "object": obj, "occurrences": v["count"], "total_rows": v["total_rows"]}
        for (op, obj), v in dml_summary_map.items()
    ]

    involved = involved_components(execution_units, exceptions, index_ids)
    async_jobs = tracker.result(header, execution_units)

    return {
        "header": header,
        "execution_units": [{"label": u["label"], "depth": u["depth"], "had_exception": u["had_exception"]} for u in execution_units],
        "exceptions": exceptions,
        "soql_summary": sorted(soql_summary, key=lambda x: -x["occurrences"]),
        "dml_summary": sorted(dml_summary, key=lambda x: -x["occurrences"]),
        "callouts": callouts, "user_debug": user_debug, "validation_failures": validation_failures,
        "flow_events": flow_events, "limits_final": limits_final,
        "limits_by_namespace": limits_by_namespace, "involved_components": involved,
        **({"async_jobs": async_jobs} if async_jobs else {}),
    }
