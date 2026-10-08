"""Fit a normalized-log tool result into the chat's tool-result budget.

Why this exists: chat._truncate used kb_lookup.shape(), which was written for
component cards. It trims LIST sections item by item but treats any other
over-budget value as "omitted". A log result is {"meta": ..., "normalized_log":
{...}} -- one nested dict -- so every log over the budget (~24 KB, i.e. most
real CPQ logs) reached the model as `"normalized_log": {"omitted": true}`.
The model, correctly, refused to do an RCA on nothing.

This shaper descends into normalized_log and trims it section by section:

1. Lossless-ish compaction first: SOQL SELECT lists (often 5 KB of field
   names) are collapsed to "<N fields>", the redundant raw_example is dropped
   for those, and consecutive identical execution units are run-length
   collapsed into one entry with `repeat`.
2. Small dict sections (header, limits_final, limits_by_namespace) are kept
   whole -- they are the cheapest, highest-signal evidence.
3. List sections share the remaining budget fairly (water-filling), so one
   1,700-unit execution tree cannot starve exceptions or SOQL. Each section
   keeps the items that matter most for an RCA: units that threw, the
   heaviest queries/DML, the debug lines nearest the failure.
4. A `_truncated` note names every trimmed section with total/shown counts
   and tells the model exactly how to page the rest with get_normalized_log.
"""

import copy
import json
import re

# Sections in the order an RCA reads them. Unknown sections go after these.
SECTION_ORDER = ("header", "exceptions", "async_jobs", "limits_final", "limits_by_namespace",
                 "validation_failures", "flow_events", "involved_components",
                 "dml_summary", "callouts", "execution_units", "soql_summary",
                 "user_debug")

# How each list section chooses what to keep when it has to be cut.
#   head   -- first N in log order
#   tail   -- last N (nearest the end of the transaction / the failure)
#   heavy  -- largest total_rows / occurrences first
#   units  -- every unit that threw, plus depth-0 units, then log order
SELECTION = {
    "exceptions": "head",
    "validation_failures": "head",
    "flow_events": "head",
    "involved_components": "head",
    "callouts": "head",
    "dml_summary": "heavy",
    "soql_summary": "heavy",
    "execution_units": "units",
    "user_debug": "tail",
}

_NOTE_RESERVE = 1400          # room for the _truncated note itself
_FIELD_LIST_MIN = 200         # only collapse SELECT lists longer than this


def _size(v):
    return len(json.dumps(v, default=str))


def is_log_result(result):
    return isinstance(result, dict) and isinstance(result.get("normalized_log"), dict)


# ---------- compaction ----------

def _top_level_from(sql):
    """Index of the top-level FROM (paren depth 0), or -1. A naive regex stops
    at the FROM of a child subquery."""
    depth, up = 0, sql.upper()
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and up.startswith(" FROM ", i):
            return i
        i += 1
    return -1


_MISSING_FIELD_RE = re.compile(r"without querying the requested field:\s*([\w.]+?)\.(\w+)", re.I)


def watched_fields(normalized):
    """{object_lower: {field_lower: field}} named by 'SObject row was retrieved
    via SOQL without querying the requested field: Obj.Field' exceptions --
    the one RCA where the SELECT list itself is the evidence."""
    out = {}
    for e in normalized.get("exceptions") or []:
        msg = e.get("message") if isinstance(e, dict) else e
        for obj, field in _MISSING_FIELD_RE.findall(str(msg or "")):
            out.setdefault(obj.lower(), {})[field.lower()] = field
    return out


def compact_soql_signature(sig, obj=None, watched=None):
    """'SELECT a,b,...,zz FROM X WHERE ...' -> 'SELECT <87 fields> FROM X WHERE ...'.
    If an exception names a field on this object as not queried, the label
    says whether this SELECT list contains it. Returns (signature, changed)."""
    if not isinstance(sig, str) or not sig[:7].upper().startswith("SELECT "):
        return sig, False
    at = _top_level_from(sig)
    if at < 0:
        return sig, False
    fields = sig[7:at]
    if len(fields) <= _FIELD_LIST_MIN:
        return sig, False
    depth, top, cur = 0, [], ""
    for ch in fields:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            top.append(cur.strip())
            cur = ""
        else:
            cur += ch
    top.append(cur.strip())
    subq = sum(1 for f in top if f.startswith("("))
    label = f"<{len(top)} fields" + (f", incl. {subq} subquer{'y' if subq == 1 else 'ies'}" if subq else "")
    want = (watched or {}).get((obj or "").lower())
    if want:
        have = {f.lower() for f in top}
        present = [v for k, v in want.items() if k in have]
        missing = [v for k, v in want.items() if k not in have]
        if present:
            label += "; INCLUDES " + ", ".join(present)
        if missing:
            label += "; DOES NOT include " + ", ".join(missing)
    return f"SELECT {label}>{sig[at:]}", True


def _rle_units(units):
    out = []
    for u in units:
        if (out and isinstance(u, dict) and isinstance(out[-1], dict)
                and {k: v for k, v in out[-1].items() if k != "repeat"} == u):
            out[-1]["repeat"] = out[-1].get("repeat", 1) + 1
        else:
            out.append(dict(u) if isinstance(u, dict) else u)
    return out


def compact(normalized):
    """Return (compacted copy, list of what was compacted)."""
    n = copy.deepcopy(normalized)
    notes = []
    soql = n.get("soql_summary")
    if isinstance(soql, list):
        hit, watched = 0, watched_fields(n)
        for q in soql:
            if isinstance(q, dict):
                q["signature"], changed = compact_soql_signature(q.get("signature"), q.get("object"), watched)
                if changed:
                    q.pop("raw_example", None)
                    hit += 1
        if hit:
            notes.append(f"soql_summary: SELECT field lists collapsed to '<N fields>' on {hit} "
                         f"queries (object, WHERE clause, rows and counts are intact; page "
                         f"soql_summary with a small limit to see full field lists)")
    units = n.get("execution_units")
    if isinstance(units, list):
        rle = _rle_units(units)
        if len(rle) < len(units):
            notes.append(f"execution_units: {len(units)} units run-length collapsed to {len(rle)} "
                         f"(consecutive identical units carry 'repeat': N)")
            n["execution_units"] = rle
    return n, notes


# ---------- selection ----------

def _weight(item):
    if not isinstance(item, dict):
        return 0
    return (item.get("total_rows") or 0, item.get("occurrences") or 0, item.get("rows") or 0)


def _fit(items, cap, contiguous):
    """Greedy: keep items (already in preference order) while they fit.
    `contiguous` stops at the first misfit so head/tail stay gap-free (their
    items are often bare strings with no _i to mark a gap); otherwise a
    smaller later item may still take the space."""
    kept, used = [], 2
    for idx, item in items:
        c = _size(item) + 2
        if used + c > cap:
            if contiguous:
                break
            continue
        kept.append((idx, item))
        used += c
    return kept, used


def trim_list(name, items, cap):
    """Return (kept list, info dict) for one list section under `cap` chars."""
    mode = SELECTION.get(name, "head")
    indexed = list(enumerate(items))
    if mode == "tail":
        order = list(reversed(indexed))
    elif mode == "heavy":
        order = sorted(indexed, key=lambda p: _weight(p[1]), reverse=True)
    elif mode == "units":
        first = [p for p in indexed if isinstance(p[1], dict)
                 and (p[1].get("had_exception") or p[1].get("depth") == 0)]
        seen = {i for i, _ in first}
        order = first + [p for p in indexed if p[0] not in seen]
    else:
        order = indexed
    if mode in ("units", "heavy"):
        # _i = position in the full section, for paging. Added before fitting
        # so its bytes are counted.
        order = [(i, dict(it, _i=i) if isinstance(it, dict) else it) for i, it in order]
    kept, used = _fit(order, cap, contiguous=mode in ("head", "tail"))
    if mode == "heavy":
        kept.sort(key=lambda p: _weight(p[1]), reverse=True)
    else:
        kept.sort(key=lambda p: p[0])            # back to log order
    out = [item for _, item in kept]
    info = {"total": len(items), "shown": len(out),
            "selection": {"head": "first in log order",
                          "tail": "last in log order (nearest the end of the transaction)",
                          "heavy": "heaviest first (by total_rows / occurrences); _i = index in the full list",
                          "units": "every unit that threw and every depth-0 unit, then log order; "
                                   "_i = index in the full list"}[mode]}
    return out, info, used


# ---------- main entry ----------

def shape_log_result(result, max_chars, log_id=None):
    """Fit {"meta":..., "normalized_log": {...}, ...} into `max_chars` of JSON.
    Returns the result unchanged if it already fits. The per-section budget
    is an estimate (the note's length varies), so tighten and retry on an
    overshoot rather than hand back something over the cap."""
    if not is_log_result(result) or _size(result) <= max_chars:
        return result
    target = max_chars
    for _ in range(5):
        out = _shape_once(result, target, max_chars, log_id)
        over = _size(out) - max_chars
        if over <= 0:
            return out
        target -= over + 500
    return out


def _shape_once(result, max_chars, reported_max, log_id):
    original_size = _size(result)
    log_id = log_id or (result.get("log_id")
                        or (result.get("meta") or {}).get("log_id")
                        or (result.get("meta") or {}).get("source_log_id"))

    out = {k: v for k, v in result.items() if k != "normalized_log"}
    meta = out.get("meta")
    normalized, compacted = compact(result["normalized_log"])
    if isinstance(meta, dict) and meta.get("involved_components") == normalized.get("involved_components"):
        meta = dict(meta)
        meta.pop("involved_components", None)    # duplicated inside normalized_log
        out["meta"] = meta

    trimmed = {}
    if _size(dict(out, normalized_log=normalized)) + 200 > max_chars:
        budget = max_chars - _size(out) - _NOTE_RESERVE
        keys = ([k for k in SECTION_ORDER if k in normalized]
                + [k for k in normalized if k not in SECTION_ORDER])
        shaped = {}
        # Non-list sections (header, limits) whole, unless one is absurdly big.
        for k in keys:
            v = normalized[k]
            if isinstance(v, list):
                continue
            cost = _size(v) + len(k) + 6
            if cost <= max(budget // 2, 0):
                shaped[k] = v
                budget -= cost
            else:
                trimmed[k] = {"omitted": True, "chars": cost}
        # List sections share what is left: water-fill, smallest first.
        lists = [k for k in keys if isinstance(normalized[k], list)]
        caps, left = {}, max(budget, 0)
        pending = sorted(lists, key=lambda k: _size(normalized[k]))
        while pending:
            share = left // len(pending)
            k = pending.pop(0)
            need = _size(normalized[k]) + len(k) + 6
            caps[k] = min(need, share)
            left -= caps[k]
        for k in lists:
            v = normalized[k]
            if _size(v) + len(k) + 6 <= caps[k]:
                shaped[k] = v
            else:
                shaped[k], info, _ = trim_list(k, v, caps[k] - len(k) - 6)
                trimmed[k] = info
        normalized = {k: shaped[k] for k in keys if k in shaped}

    out["normalized_log"] = normalized
    if trimmed or compacted:
        if log_id:
            how = (f"Call get_normalized_log(log_id='{log_id}', sections=['<section>'], offset=<n>, "
                   f"limit=<m>) to page a trimmed section in log order (offset is the index in the "
                   f"full section; _i on a shown item is that index). Ask for one section at a time.")
        else:
            how = ("This log is not stored, so it cannot be paged. Re-run normalize_log with store=true "
                   "to get a log_id, then page a section with get_normalized_log(log_id, sections=[...], "
                   "offset, limit).")
        note = {
            "reason": (f"TRUNCATED: this log result is {original_size} chars; shaped to fit {reported_max}. "
                       f"Exceptions, limits and the units that threw are kept first. Reason over what is "
                       f"shown, say which trimmed section your conclusion depends on, and page it "
                       f"before concluding something is absent."),
            "sections": trimmed,
            "how_to_get_the_rest": how,
        }
        if compacted:
            note["compacted"] = compacted
        out["_truncated"] = note
    return out
