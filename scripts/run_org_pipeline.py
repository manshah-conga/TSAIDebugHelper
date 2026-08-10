"""
run_org_pipeline.py — the single command to onboard a new org, or refresh
an existing one when a fresh metadata backup lands.

This is the "many orgs keep coming" half of the procedure. It expects a
metadata backup with the standard Salesforce Metadata API layout
(classes/, triggers/, flows/, lwc/, objects/ subfolders — the same shape
Conga Discovery / a sfdx retrieve / a Workbench export produces) and:

  1. Hashes every source file (LWC bundles are hashed as one logical
     component) and diffs against the manifest from the last run for
     this org, so every extraction records what was added, changed, or
     removed since last time — not just a snapshot.
  2. Re-runs the static extractors (cheap: sub-second even at ~1,100
     components) to rebuild that org's knowledge base.
  3. Registers/updates the org in a shared multi-org registry.json, so
     "how many orgs do we track, and when was each last refreshed" is a
     single file read instead of a filesystem crawl.
  4. Appends one line to that org's extraction_log.json changelog.

The output of step 1 (first_seen / last_changed per component) is what
build_rca_context.py later uses to flag "this class was modified 3 days
before the incident" — a common real RCA lead — which is why hashing
happens here, once, rather than being recomputed at incident time.

Usage:
    python run_org_pipeline.py <org_id> <org_name> <backup_dir> <orgs_root>

Example:
    python run_org_pipeline.py ibm_devfeb26 "IBM - DevFeb26" \\
        "/path/to/BackupData" ./orgs
"""
import os
import sys
import json
import subprocess

from common import sha256_file, sha256_concat, iso_now, write_json

HERE = os.path.dirname(os.path.abspath(__file__))


def hash_backup(backup_dir):
    hashes = {}
    classes_dir = os.path.join(backup_dir, "classes")
    triggers_dir = os.path.join(backup_dir, "triggers")
    flows_dir = os.path.join(backup_dir, "flows")
    lwc_dir = os.path.join(backup_dir, "lwc")

    if os.path.isdir(classes_dir):
        for fn in os.listdir(classes_dir):
            if fn.endswith(".cls"):
                hashes[f"classes/{fn}"] = sha256_file(os.path.join(classes_dir, fn))
    if os.path.isdir(triggers_dir):
        for fn in os.listdir(triggers_dir):
            if fn.endswith(".trigger"):
                hashes[f"triggers/{fn}"] = sha256_file(os.path.join(triggers_dir, fn))
    if os.path.isdir(flows_dir):
        for fn in os.listdir(flows_dir):
            if fn.endswith(".flow"):
                hashes[f"flows/{fn}"] = sha256_file(os.path.join(flows_dir, fn))
    if os.path.isdir(lwc_dir):
        for name in os.listdir(lwc_dir):
            comp_dir = os.path.join(lwc_dir, name)
            if os.path.isdir(comp_dir):
                files = [os.path.join(comp_dir, f) for f in os.listdir(comp_dir)]
                if files:
                    hashes[f"lwc/{name}"] = sha256_concat(files)
    return hashes


def diff_manifest(new_hashes, prev_manifest):
    now = iso_now()
    merged = {}
    added = changed = unchanged = 0
    for key, h in new_hashes.items():
        prev = prev_manifest.get(key)
        if prev is None:
            merged[key] = {"hash": h, "first_seen": now, "last_changed": now}
            added += 1
        elif prev["hash"] == h:
            merged[key] = prev
            unchanged += 1
        else:
            merged[key] = {"hash": h, "first_seen": prev.get("first_seen", now), "last_changed": now}
            changed += 1
    removed_keys = [k for k in prev_manifest if k not in new_hashes]
    return merged, {"added": added, "changed": changed, "unchanged": unchanged, "removed": len(removed_keys)}, removed_keys


def main():
    org_id, org_name, backup_dir, orgs_root = sys.argv[1:5]

    org_dir = os.path.join(orgs_root, org_id)
    kb_dir = os.path.join(org_dir, "knowledge_base")
    os.makedirs(kb_dir, exist_ok=True)
    os.makedirs(os.path.join(org_dir, "incidents"), exist_ok=True)

    manifest_path = os.path.join(kb_dir, "file_hashes.json")
    prev_manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

    new_hashes = hash_backup(backup_dir)
    merged_manifest, counts, removed_keys = diff_manifest(new_hashes, prev_manifest)
    write_json(manifest_path, merged_manifest)

    # re-run the static extractors — always, since it's cheap; this keeps
    # the knowledge base a straight rebuild rather than a hand-merged patch,
    # which is far less error-prone at this file-count scale.
    classes_dir = os.path.join(backup_dir, "classes")
    triggers_dir = os.path.join(backup_dir, "triggers")
    flows_dir = os.path.join(backup_dir, "flows")
    lwc_dir = os.path.join(backup_dir, "lwc")
    objects_dir = os.path.join(backup_dir, "objects")

    subprocess.run([sys.executable, os.path.join(HERE, "extract_apex.py"),
                     classes_dir, triggers_dir, objects_dir, kb_dir], check=True)
    if os.path.isdir(flows_dir):
        subprocess.run([sys.executable, os.path.join(HERE, "extract_flow.py"),
                         flows_dir, kb_dir], check=True)
    if os.path.isdir(lwc_dir):
        subprocess.run([sys.executable, os.path.join(HERE, "extract_lwc.py"),
                         lwc_dir, kb_dir], check=True)
    subprocess.run([sys.executable, os.path.join(HERE, "build_index.py"),
                     kb_dir, kb_dir], check=True)

    # changelog
    log_path = os.path.join(org_dir, "extraction_log.json")
    log = json.load(open(log_path)) if os.path.exists(log_path) else []
    log.append({
        "timestamp": iso_now(),
        "backup_dir": backup_dir,
        "files_seen": len(new_hashes),
        **counts,
        "removed_files": removed_keys,
    })
    write_json(log_path, log)

    # multi-org registry
    registry_path = os.path.join(orgs_root, "registry.json")
    registry = json.load(open(registry_path)) if os.path.exists(registry_path) else {}
    org_stats = json.load(open(os.path.join(kb_dir, "org_stats.json")))
    registry[org_id] = {
        "name": org_name,
        "backup_source_path": backup_dir,
        "first_onboarded_at": registry.get(org_id, {}).get("first_onboarded_at", iso_now()),
        "last_extracted_at": iso_now(),
        "component_counts": org_stats["counts"],
        "extraction_runs": len(log),
    }
    write_json(registry_path, registry)

    print(f"Org '{org_id}' ({org_name}): {counts['added']} added, {counts['changed']} changed, "
          f"{counts['removed']} removed, {counts['unchanged']} unchanged  "
          f"(of {len(new_hashes)} total components).")
    print(f"Knowledge base: {kb_dir}")
    if counts["added"] == 0 and counts["changed"] == 0 and counts["removed"] == 0:
        print("No source changes detected since the last run for this org.")


if __name__ == "__main__":
    main()
