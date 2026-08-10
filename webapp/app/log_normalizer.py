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
LIMIT_LINE_RE = re.compile(r"^\s*Number of (.+?):\s*(\d+)\s*out of\s*(\d+)\s*$")
CLASS_METHOD_STACK_RE = re.compile(r"^Class\.([\w.]+?)(?:\.(\w+))?:\s*line\s*(\d+)")
TRIGGER_STACK_RE = re.compile(r"^Trigger\.(\w+):\s*line\s*(\d+)")
NORMALIZE_LITERAL_RE = re.compile(r"'[^']*'|:\w+|\b\d+\b")


def normalize_signature(text):
    return NORMALIZE_LITERAL_RE.sub("?", text).strip()


def truncate(s, n=180):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


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


def parse_log_text(text, index_ids=None):
    lines = text.splitlines()
    header = parse_debug_level_header(lines[0] if lines else "")

    code_unit_stack, execution_units = [], []
    soql_open, dml_open = [], []
    soql_events, dml_events = [], []
    callouts, user_debug, exceptions, flow_events, validation_failures = [], [], [], [], []
    limits_final = {}
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
        elif evt == "CODE_UNIT_FINISHED":
            if code_unit_stack:
                code_unit_stack.pop()
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
        elif evt == "FATAL_ERROR":
            in_fatal_stack = True
            current_stack_lines = []
            if current_exception is None:
                msg = parts[-1] if len(parts) >= 3 else raw
                current_exception = {"type": msg.split(":")[0].strip(), "message": msg, "stack": []}
                exceptions.append(current_exception)

    for raw in lines:
        m = LIMIT_LINE_RE.match(raw)
        if m:
            limits_final[m.group(1).strip()] = {"used": int(m.group(2)), "max": int(m.group(3))}

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

    involved = set()
    name_pat = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{2,})\b")
    text_blob = " ".join(
        [u["label"] for u in execution_units] + [e["message"] for e in exceptions]
        + [" ".join(e.get("stack", [])) for e in exceptions]
    )
    for m in name_pat.finditer(text_blob):
        tok = m.group(1)
        if index_ids and tok in index_ids:
            involved.add(tok)
        elif not index_ids and (tok.startswith("ibmc") or tok.startswith("itcc") or tok.startswith("APTS")):
            involved.add(tok)

    return {
        "header": header,
        "execution_units": [{"label": u["label"], "depth": u["depth"], "had_exception": u["had_exception"]} for u in execution_units],
        "exceptions": exceptions,
        "soql_summary": sorted(soql_summary, key=lambda x: -x["occurrences"]),
        "dml_summary": sorted(dml_summary, key=lambda x: -x["occurrences"]),
        "callouts": callouts, "user_debug": user_debug, "validation_failures": validation_failures,
        "flow_events": flow_events, "limits_final": limits_final, "involved_components": sorted(involved),
    }
