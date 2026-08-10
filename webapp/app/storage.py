"""
All disk I/O for the web app lives here, deliberately, so it's one place
to audit for the "only normalized JSON, never raw data" rule. Every write
in this module is a JSON document derived by the extractors/index
builder/log normalizer -- nothing here ever accepts raw Apex source, raw
Flow metadata, raw LWC file text, or a raw debug log body.
"""
import os
import json
import glob

DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ORGS_ROOT = os.path.join(DATA_ROOT, "orgs")
REGISTRY_PATH = os.path.join(DATA_ROOT, "registry.json")
# Standalone (org-independent) normalized-log library. A log lands here when a
# user just wants to normalize + keep a log for reference without tying it to a
# connected org's knowledgebase. As everywhere else, only the derived
# normalized JSON is stored -- never the raw log text.
LOGS_ROOT = os.path.join(DATA_ROOT, "normalized_logs")
# Auth store: local user accounts (with salted password hashes) and API
# tokens (stored as hashes, never in the clear). This is app-access data, not
# anything fetched from Salesforce, so it does not affect the "only normalized
# JSON, never raw org data" guarantee.
AUTH_ROOT = os.path.join(DATA_ROOT, "auth")
USERS_PATH = os.path.join(AUTH_ROOT, "users.json")
TOKENS_PATH = os.path.join(AUTH_ROOT, "tokens.json")


def _ensure_dirs():
    os.makedirs(ORGS_ROOT, exist_ok=True)


def load_users():
    return read_json(USERS_PATH, {})


def save_users(users):
    write_json(USERS_PATH, users)


def load_tokens():
    return read_json(TOKENS_PATH, {})


def save_tokens(tokens):
    write_json(TOKENS_PATH, tokens)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def org_dir(org_id):
    return os.path.join(ORGS_ROOT, org_id)


def kb_dir(org_id):
    return os.path.join(org_dir(org_id), "knowledge_base")


def incidents_dir(org_id):
    return os.path.join(org_dir(org_id), "incidents")


def load_registry():
    _ensure_dirs()
    return read_json(REGISTRY_PATH, {})


def save_registry(registry):
    write_json(REGISTRY_PATH, registry)


def load_kb(org_id):
    """Loads every knowledgebase JSON document for an org. Returns None
    for any file not yet written (e.g. mid-fetch)."""
    d = kb_dir(org_id)
    return {
        "org_index": read_json(os.path.join(d, "org_index.json"), {}),
        "object_touch_map": read_json(os.path.join(d, "object_touch_map.json"), {}),
        "call_graph": read_json(os.path.join(d, "call_graph.json"), {"calls": {}, "called_by": {}}),
        "field_touch_map": read_json(os.path.join(d, "field_touch_map.json"), {}),
        "org_stats": read_json(os.path.join(d, "org_stats.json"), {}),
        "file_hashes": read_json(os.path.join(d, "file_hashes.json"), {}),
    }


def save_kb(org_id, index_result, file_hashes):
    d = kb_dir(org_id)
    write_json(os.path.join(d, "org_index.json"), index_result["org_index"])
    write_json(os.path.join(d, "object_touch_map.json"), index_result["object_touch_map"])
    write_json(os.path.join(d, "call_graph.json"), index_result["call_graph"])
    write_json(os.path.join(d, "field_touch_map.json"), index_result["field_touch_map"])
    write_json(os.path.join(d, "org_stats.json"), index_result["org_stats"])
    write_json(os.path.join(d, "file_hashes.json"), file_hashes)


def load_known_issues(org_id):
    return read_json(os.path.join(incidents_dir(org_id), "known_issues_index.json"), {})


def save_known_issues(org_id, known):
    write_json(os.path.join(incidents_dir(org_id), "known_issues_index.json"), known)


def save_incident(org_id, incident_id, normalized_log, context_pack, meta):
    d = os.path.join(incidents_dir(org_id), incident_id)
    write_json(os.path.join(d, "normalized_log.json"), normalized_log)
    write_json(os.path.join(d, "rca_context_pack.json"), context_pack)
    write_json(os.path.join(d, "meta.json"), meta)


def list_incidents(org_id):
    pattern = os.path.join(incidents_dir(org_id), "*")
    out = []
    for path in sorted(glob.glob(pattern), reverse=True):
        if os.path.isdir(path):
            meta = read_json(os.path.join(path, "meta.json"))
            if meta:
                out.append(meta)
    return out


def load_incident(org_id, incident_id):
    d = os.path.join(incidents_dir(org_id), incident_id)
    if not os.path.isdir(d):
        return None
    return {
        "meta": read_json(os.path.join(d, "meta.json")),
        "normalized_log": read_json(os.path.join(d, "normalized_log.json")),
        "rca_context_pack": read_json(os.path.join(d, "rca_context_pack.json")),
    }


# ---------- standalone normalized-log library (org-independent) ----------

def _log_dir(log_id):
    return os.path.join(LOGS_ROOT, log_id)


def save_normalized_log(log_id, normalized, meta):
    """Persist an org-independent normalized log. Only the derived JSON
    (`normalized`) and its metadata are written -- never the raw log."""
    d = _log_dir(log_id)
    write_json(os.path.join(d, "normalized_log.json"), normalized)
    write_json(os.path.join(d, "meta.json"), meta)


def list_normalized_logs():
    out = []
    for path in sorted(glob.glob(os.path.join(LOGS_ROOT, "*")), reverse=True):
        if os.path.isdir(path):
            meta = read_json(os.path.join(path, "meta.json"))
            if meta:
                out.append(meta)
    return out


def load_normalized_log(log_id):
    d = _log_dir(log_id)
    if not os.path.isdir(d):
        return None
    return {
        "meta": read_json(os.path.join(d, "meta.json")),
        "normalized_log": read_json(os.path.join(d, "normalized_log.json")),
    }
