"""
Incident filing + recurrence tracking, same logic as the standalone
new_incident.py script, refactored for the web app: no CLI arg parsing,
just plain functions the FastAPI routes call.
"""
import re
import hashlib
import datetime


def iso_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

STACK_FRAME_RE = re.compile(r"^Class\.([\w.]+?)(?:\.\w+)?:", re.IGNORECASE)
NORMALIZE_LITERAL_RE = re.compile(r"'[^']*'|:\w+|\b\d+\b")


def _normalize_signature(text):
    return NORMALIZE_LITERAL_RE.sub("?", text).strip()


def exception_signature(exceptions):
    if not exceptions:
        return None
    exc = exceptions[0]
    norm_msg = _normalize_signature(exc.get("message", ""))
    frame = ""
    for line in exc.get("stack", []):
        m = STACK_FRAME_RE.match(line.strip())
        if m:
            frame = m.group(1)
            break
    key = f"{exc.get('type','')}|{norm_msg}|{frame}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def field_signature(field):
    return hashlib.sha256(f"field_report|{field}".encode("utf-8")).hexdigest()[:16]


def file_incident(known, incident_id, normalized, field=None):
    """Mutates `known` (the known_issues_index dict) in place and returns
    (signature, recurrence, prior_dict_or_None, signature_source)."""
    signature = exception_signature(normalized.get("exceptions", []))
    sig_source = "exception"
    if signature is None and field:
        signature = field_signature(field)
        sig_source = "field_report"

    if not signature:
        return None, False, None, None

    now = iso_now()
    if signature in known:
        prior = dict(known[signature])
        known[signature]["last_seen"] = now
        known[signature]["occurrences"] += 1
        known[signature]["incident_ids"].append(incident_id)
        return signature, True, prior, sig_source

    if sig_source == "exception":
        exc = normalized["exceptions"][0]
        entry = {"kind": "exception", "type": exc.get("type"), "message_sample": exc.get("message"),
                 "stack_sample": exc.get("stack", [])}
    else:
        entry = {"kind": "field_report", "field": field}
    known[signature] = {
        **entry, "first_seen": now, "last_seen": now, "occurrences": 1,
        "incident_ids": [incident_id], "resolution": None,
    }
    return signature, False, None, sig_source
