"""
normalize_log.py — condenses a raw Salesforce Apex debug log into a small,
structured, high-signal JSON document for RCA, instead of feeding the raw
log (which is dominated by METHOD_ENTRY/EXIT, VARIABLE_ASSIGNMENT and
per-line CUMULATIVE_LIMIT_USAGE noise) to an LLM.

Kept:  EXCEPTION_THROWN / FATAL_ERROR (+ stack), top-level CODE_UNIT
       boundaries (trigger/flow/class entry points), SOQL/DML executed
       (deduplicated + counted, not printed once per loop iteration),
       callouts, USER_DEBUG statements, flow interview start/end and
       fault events, validation-rule failures, and the *final* governor
       limit snapshot only.
Dropped: METHOD_ENTRY/METHOD_EXIT (used only to maintain the call stack
       in memory), VARIABLE_ASSIGNMENT, STATEMENT_EXECUTE, HEAP_ALLOCATE,
       every intermediate CUMULATIVE_LIMIT_USAGE line, and any block that
       repeats identically (e.g. the same query fired 500 times in a
       loop) is collapsed to one example + a count.

Usage:
    python normalize_log.py <raw_log_path> <out_dir> [--index org_index.json]
"""
import os
import re
import sys
import json
from collections import defaultdict

from common import write_json, truncate

DML_BEGIN_RE = re.compile(r"Op:(\w+)\|Type:(\w+)\|Rows:(\d+)")
SOQL_END_ROWS_RE = re.compile(r"Rows:(\d+)")
LIMIT_LINE_RE = re.compile(r"^\s*Number of (.+?):\s*(\d+)\s*out of\s*(\d+)\s*$")
CLASS_METHOD_STACK_RE = re.compile(r"^Class\.([\w.]+?)(?:\.(\w+))?:\s*line\s*(\d+)")
TRIGGER_STACK_RE = re.compile(r"^Trigger\.(\w+):\s*line\s*(\d+)")
NORMALIZE_LITERAL_RE = re.compile(r"'[^']*'|:\w+|\b\d+\b")


def normalize_signature(text):
    """Collapse bind variables / literals so 500 loop iterations of the
    same query/DML collapse into a single signature for counting."""
    return NORMALIZE_LITERAL_RE.sub("?", text).strip()


def parse_log(path, index_ids=None):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\n") for l in f]

    header = lines[0] if lines else ""

    code_unit_stack = []       # top-level + nested code units
    execution_units = []       # flattened top-level units, in order
    soql_open = []             # stack of open SOQL_EXECUTE_BEGIN
    dml_open = []               # stack of open DML_BEGIN
    soql_events = []            # completed {signature, object, rows}
    dml_events = []              # completed {op, object, rows}
    callouts = []
    user_debug = []
    exceptions = []
    flow_events = []
    validation_failures = []
    limits_final = {}
    in_fatal_stack = False
    current_stack_lines = []
    current_exception = None

    for raw in lines[1:]:
        if not raw.strip():
            continue
        parts = raw.split("|")

        if in_fatal_stack:
            m1 = CLASS_METHOD_STACK_RE.match(raw.strip())
            m2 = TRIGGER_STACK_RE.match(raw.strip())
            if m1 or m2:
                current_stack_lines.append(raw.strip())
                continue
            else:
                in_fatal_stack = False
                if current_exception is not None:
                    current_exception["stack"] = current_stack_lines
                    current_stack_lines = []
                    current_exception = None

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
                "object": from_m.group(1) if from_m else None,
                "rows": rows,
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

        elif evt == "LIMIT_USAGE_FOR_NS":
            continue  # handled by scanning raw text block below

    # governor limits: scan the whole file for "Number of X: N out of M" and
    # keep only the LAST occurrence of each limit name (the final snapshot)
    for raw in lines:
        m = LIMIT_LINE_RE.match(raw)
        if m:
            limits_final[m.group(1).strip()] = {"used": int(m.group(2)), "max": int(m.group(3))}

    # collapse repeated SOQL / DML into signature groups
    def collapse(events, key_fields, group_key):
        groups = defaultdict(lambda: {"count": 0, "total_rows": 0, "example": None})
        for e in events:
            k = e.get(group_key)
            g = groups[k]
            g["count"] += 1
            g["total_rows"] += e.get("rows") or 0
            if g["example"] is None:
                g["example"] = e
        out = []
        for k, g in groups.items():
            item = dict(g["example"])
            item["occurrences"] = g["count"]
            item["total_rows"] = g["total_rows"]
            out.append(item)
        return out

    # a single unhandled exception is commonly logged once per nested
    # code-unit boundary it unwinds through (same type/message/stack
    # repeated) - collapse those into one entry with an occurrence count
    # instead of surfacing apparent duplicates to the model.
    exc_groups = {}
    exc_order = []
    for e in exceptions:
        key = (e["type"], e["message"], tuple(e.get("stack", [])))
        if key not in exc_groups:
            exc_groups[key] = dict(e)
            exc_groups[key]["occurrences"] = 0
            exc_order.append(key)
        exc_groups[key]["occurrences"] += 1
    exceptions = [exc_groups[k] for k in exc_order]

    soql_summary = collapse(soql_events, None, "signature")
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
        [u["label"] for u in execution_units]
        + [e["message"] for e in exceptions]
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
        "execution_units": [
            {"label": u["label"], "depth": u["depth"], "had_exception": u["had_exception"]}
            for u in execution_units
        ],
        "exceptions": exceptions,
        "soql_summary": sorted(soql_summary, key=lambda x: -x["occurrences"]),
        "dml_summary": sorted(dml_summary, key=lambda x: -x["occurrences"]),
        "callouts": callouts,
        "user_debug": user_debug,
        "validation_failures": validation_failures,
        "flow_events": flow_events,
        "limits_final": limits_final,
        "involved_components": sorted(involved),
    }


def main():
    args = sys.argv[1:]
    index_path = None
    if "--index" in args:
        i = args.index("--index")
        index_path = args[i + 1]
        del args[i:i + 2]
    log_path, out_dir = args[0], args[1]

    index_ids = None
    if index_path and os.path.exists(index_path):
        index_ids = set(json.load(open(index_path)).keys())

    result = parse_log(log_path, index_ids)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "normalized_log.json")
    write_json(out_path, result)

    raw_size = os.path.getsize(log_path)
    norm_size = os.path.getsize(out_path)
    print(f"Raw log: {raw_size:,} bytes -> Normalized: {norm_size:,} bytes "
          f"({raw_size / max(norm_size, 1):.1f}x smaller)")


if __name__ == "__main__":
    main()
