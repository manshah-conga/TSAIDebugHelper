"""
new_incident.py — the single command to run when a new issue comes in for
an org that's already been onboarded (run_org_pipeline.py has already
built its knowledge_base/).

This is the "same org, new issue, keeps coming" half of the procedure,
and it now covers two shapes of incident:

  1. A debug log with an exception. It normalizes the log, assembles the
     RCA context pack (flagging any recently-changed component along the
     way), and computes a signature for the exception (type + normalized
     message + the first stack frame's class.method, deliberately
     ignoring line numbers) checked against known_issues_index.json.

  2. A field reported as having the wrong value with NO exception and
     often no log at all -- e.g. "Increment_Adjustment__c came out wrong
     after this transaction." Pass --field <FieldApiName__c> and this
     looks the field up in field_touch_map.json (built by build_index.py
     from every field write extract_apex.py found across the org) and
     folds every writer -- ranked by risk, e.g. "written from a static,
     never-cleared Map" -- into the context pack, using nothing but the
     knowledgebase. Recurrence is still tracked, keyed off the field name
     itself instead of an exception signature.

--log and --field can be combined: a log narrows the blast radius (which
transaction, which objects), --field adds writers the log's own exception
list wouldn't surface because nothing in this transaction actually threw.

If the signature has been seen before for this org, the incident is
flagged as a RECURRENCE with a pointer to every prior occurrence (and any
resolution someone has since recorded against it) instead of starting
RCA over from a blank page. If not, it's logged as a new issue so the
*next* occurrence gets that benefit.

Usage:
    python new_incident.py <org_id> <orgs_root> --log <raw_log_path> [--label text]
    python new_incident.py <org_id> <orgs_root> --field <FieldApiName__c> [--label text]
    python new_incident.py <org_id> <orgs_root> --log <path> --field <FieldApiName__c> [--label text]

Recording a resolution once RCA is confirmed (so future recurrences see it):
    python new_incident.py --resolve <org_id> <orgs_root> <signature> "<resolution text>"
"""
import os
import re
import sys
import json
import hashlib
import datetime

from common import write_json, iso_now
import normalize_log
from build_rca_context import assemble_context
from find_field_writers import lookup_field_writers

STACK_FRAME_RE = re.compile(r"^Class\.([\w.]+?)(?:\.\w+)?:", re.IGNORECASE)


def exception_signature(exceptions):
    if not exceptions:
        return None
    exc = exceptions[0]
    norm_msg = normalize_log.normalize_signature(exc.get("message", ""))
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


def load_json(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def run_resolve(org_id, orgs_root, signature, resolution_text):
    org_dir = os.path.join(orgs_root, org_id)
    known_path = os.path.join(org_dir, "incidents", "known_issues_index.json")
    known = load_json(known_path, {})
    if signature not in known:
        print(f"No known issue with signature {signature} for org '{org_id}'.")
        return
    known[signature]["resolution"] = resolution_text
    known[signature]["resolution_recorded_at"] = iso_now()
    write_json(known_path, known)
    print(f"Resolution recorded for {signature} ({known[signature]['occurrences']} occurrence(s) on file).")


def parse_args(args):
    opts = {"label": None, "log": None, "field": None}
    for flag, key in [("--label", "label"), ("--log", "log"), ("--field", "field")]:
        if flag in args:
            i = args.index(flag)
            opts[key] = args[i + 1]
            del args[i:i + 2]
    return opts, args


def main():
    args = sys.argv[1:]
    if args and args[0] == "--resolve":
        _, org_id, orgs_root, signature, resolution_text = args[:5]
        run_resolve(org_id, orgs_root, signature, resolution_text)
        return

    opts, rest = parse_args(args)
    org_id, orgs_root = rest[0], rest[1]
    raw_log_path, field, label = opts["log"], opts["field"], opts["label"]

    if not raw_log_path and not field:
        print("Provide --log <path>, --field <FieldApiName__c>, or both.")
        return

    org_dir = os.path.join(orgs_root, org_id)
    kb_dir = os.path.join(org_dir, "knowledge_base")
    incidents_dir = os.path.join(org_dir, "incidents")
    os.makedirs(incidents_dir, exist_ok=True)

    org_index = json.load(open(os.path.join(kb_dir, "org_index.json")))
    call_graph = json.load(open(os.path.join(kb_dir, "call_graph.json")))
    object_touch = json.load(open(os.path.join(kb_dir, "object_touch_map.json")))
    file_hashes = load_json(os.path.join(kb_dir, "file_hashes.json"), None)

    if raw_log_path:
        normalized = normalize_log.parse_log(raw_log_path, index_ids=set(org_index.keys()))
    else:
        normalized = {
            "header": None, "execution_units": [], "exceptions": [], "soql_summary": [],
            "dml_summary": [], "callouts": [], "user_debug": [], "validation_failures": [],
            "flow_events": [], "limits_final": {}, "involved_components": [],
        }
    context_pack = assemble_context(normalized, org_index, call_graph, object_touch, file_hashes)

    field_result = None
    if field:
        field_result = lookup_field_writers(field, kb_dir)
        context_pack["suspect_field"] = field
        context_pack["suspect_field_writers"] = field_result["writers"]
        # fold the writing components into primary_components too, so they
        # get pulled into whatever's fed to the model for RCA, same as any
        # exception-implicated component would be.
        for w in field_result["writers"]:
            cid = w["component"]
            if cid in org_index:
                context_pack["primary_components"][cid] = org_index[cid]

    signature = exception_signature(normalized.get("exceptions", []))
    sig_source = "exception"
    if signature is None and field:
        signature = field_signature(field)
        sig_source = "field_report"

    known_path = os.path.join(incidents_dir, "known_issues_index.json")
    known = load_json(known_path, {})

    now = iso_now()
    timestamp_slug = now.replace(":", "").replace("-", "")
    default_label = (os.path.splitext(os.path.basename(raw_log_path))[0] if raw_log_path else field)
    incident_id = f"{timestamp_slug}_{label or default_label}"
    incident_dir = os.path.join(incidents_dir, incident_id)
    os.makedirs(incident_dir, exist_ok=True)

    recurrence = False
    prior = None
    if signature:
        if signature in known:
            recurrence = True
            prior = dict(known[signature])
            known[signature]["last_seen"] = now
            known[signature]["occurrences"] += 1
            known[signature]["incident_ids"].append(incident_id)
        else:
            if sig_source == "exception":
                exc = normalized["exceptions"][0]
                entry = {
                    "kind": "exception",
                    "type": exc.get("type"),
                    "message_sample": exc.get("message"),
                    "stack_sample": exc.get("stack", []),
                }
            else:
                entry = {"kind": "field_report", "field": field}
            known[signature] = {
                **entry,
                "first_seen": now,
                "last_seen": now,
                "occurrences": 1,
                "incident_ids": [incident_id],
                "resolution": None,
            }
        write_json(known_path, known)

    meta = {
        "incident_id": incident_id,
        "org_id": org_id,
        "timestamp": now,
        "source_log": os.path.basename(raw_log_path) if raw_log_path else None,
        "suspect_field": field,
        "signature": signature,
        "signature_source": sig_source if signature else None,
        "recurrence": recurrence,
        "prior_occurrences": prior["occurrences"] if prior else 0,
        "prior_incident_ids": prior["incident_ids"] if prior else [],
        "prior_resolution": prior.get("resolution") if prior else None,
    }

    write_json(os.path.join(incident_dir, "normalized_log.json"), normalized)
    write_json(os.path.join(incident_dir, "rca_context_pack.json"), context_pack)
    write_json(os.path.join(incident_dir, "meta.json"), meta)

    print(f"Incident archived: {incident_dir}")
    if field and field_result is not None:
        n = len(field_result["writers"])
        high = sum(1 for w in field_result["writers"] if w["risk"] == "high")
        print(f"Field '{field}': {n} writer(s) found ({high} high-risk) -- see rca_context_pack.json's "
              f"suspect_field_writers.")
    if signature is None:
        print("No exception and no --field given -- filed without recurrence tracking.")
    elif recurrence:
        print(f"RECURRENCE: signature {signature} seen {meta['prior_occurrences']} time(s) before "
              f"(first seen {prior['first_seen']}). Prior incidents: {', '.join(meta['prior_incident_ids'][:-1])}")
        if meta["prior_resolution"]:
            print(f"Recorded resolution on file: {meta['prior_resolution']}")
        else:
            print("No resolution has been recorded for this issue yet -- "
                  "record one with new_incident.py --resolve once RCA is confirmed.")
    else:
        print(f"NEW ISSUE: signature {signature} not seen before for this org.")


if __name__ == "__main__":
    main()
