"""
find_field_writers.py — answers "field X had the wrong value, and nothing
threw an exception" using only the knowledgebase, no raw source access.

This is the direct fix for the gap found on the BoxSBJuly26 / pcbtest1
incident: a business user reported Increment_Adjustment__c came out wrong
after a CPQ pricing transaction, but the debug log had zero exceptions
tied to that field, so there was nothing for normalize_log.py or
build_rca_context.py to key off of. The only way to find the cause was to
get raw file access and grep CPQ_PricingCallBack.cls by hand.

This script does that lookup from the knowledgebase alone: it reads
field_touch_map.json (built by build_index.py from every field write
extract_apex.py found) and reports every component that writes the given
field, ranked by risk -- "high" meaning the value is pulled from a static
collection in that same class that is never cleared or reset (the exact
shape of the real bug found), "medium" for other map/dictionary-driven
writes, "low" for a plain direct assignment.

Usage:
    python find_field_writers.py <org_id> <FieldApiName__c> <orgs_root>
"""
import os
import sys
import json
import datetime

from common import write_json


def lookup_field_writers(field, kb_dir):
    """Reusable core: returns {field, writers:[...]} with card + recency
    info merged in. Used by this script's CLI and by new_incident.py's
    --field option."""
    field_touch = json.load(open(os.path.join(kb_dir, "field_touch_map.json")))
    org_index = json.load(open(os.path.join(kb_dir, "org_index.json")))
    hashes_path = os.path.join(kb_dir, "file_hashes.json")
    file_hashes = json.load(open(hashes_path)) if os.path.exists(hashes_path) else {}

    now = datetime.datetime.utcnow()
    writers = field_touch.get(field, [])
    enriched = []
    for w in writers:
        card = org_index.get(w["component"], {})
        key = f"classes/{card.get('file')}" if card.get("type") in ("ApexClass", "ApexInterface", "ApexEnum") \
            else f"triggers/{card.get('file')}" if card.get("type") == "ApexTrigger" else None
        last_changed = file_hashes.get(key, {}).get("last_changed") if key else None
        age_days = None
        if last_changed:
            age_days = round((now - datetime.datetime.strptime(last_changed, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 86400, 1)
        enriched.append({
            **w,
            "card_type": card.get("type"),
            "card_file": card.get("file"),
            "last_changed": last_changed,
            "age_days": age_days,
        })
    return {"field": field, "writers": enriched}


def main():
    org_id, field, orgs_root = sys.argv[1:4]
    kb_dir = os.path.join(orgs_root, org_id, "knowledge_base")
    result = lookup_field_writers(field, kb_dir)
    writers = result["writers"]

    if not writers:
        print(f"No writes to '{field}' were found in {org_id}'s knowledgebase.")
        print("This can mean: the field is genuinely never set in Apex (e.g. it's a native "
              "Salesforce formula field, or only ever set via Flow/data load), or the write "
              "uses a shape this heuristic extractor doesn't recognize (e.g. a fully dynamic "
              "field name built from a string variable). Check flow_cards.json for record-"
              "triggered flows on the field's object as a next step.")
        return

    print(f"'{field}' is written by {len(writers)} component(s) in {org_id}, ranked by risk:\n")
    for w in writers:
        print(f"[{w['risk'].upper()}] {w['component']}  ({w.get('card_type', '?')}, {w.get('card_file', '?')})")
        if w.get("reason"):
            print(f"    {w['reason']}")
        print((f"    example: {field} = {w.get('example', '<unavailable>')}")[:160])
        if w.get("last_changed"):
            print(f"    last changed: {w['last_changed']} ({w['age_days']:.0f} days ago)")
        print()


if __name__ == "__main__":
    main()
