"""
build_rca_context.py — the piece that actually saves tokens at RCA time.

Given a normalized debug log (from normalize_log.py) and an org's
knowledge base (from run_org_pipeline.py / build_index.py), this pulls
together the *minimum* context an LLM needs to do root-cause analysis:

  1. The component cards for anything named in the log (the trigger/class
     on the stack trace).
  2. One hop out in the call graph in both directions (who calls this
     component, and what does it call) — because the bad data or the
     actual failing line is often one frame away from where the
     exception surfaced.
  3. Anything else in the org that touches the same object(s) involved
     in the failing transaction (other triggers/flows/classes on the
     same object) — the classic "which automation collided with which"
     question — listed by name only (not full cards) to keep this cheap.
  4. If the org's file_hashes.json (written by run_org_pipeline.py) is
     available, which of the primary components were last modified
     recently relative to now — "this class shipped 3 days before the
     incident" is one of the most common real RCA leads, so it is
     surfaced directly instead of requiring a separate deploy-history
     lookup.

The output is a single JSON "context pack" that is small enough to hand
to an LLM alongside the normalized log for RCA + resolution, instead of
either (a) the full org knowledge base, or (b) raw Apex source.

Usage:
    python build_rca_context.py <normalized_log.json> <kb_dir> <out_dir> [--hashes file_hashes.json] [--recent-days 14]
"""
import os
import sys
import json
import datetime

from common import write_json

TYPE_TO_CATEGORY = {
    "ApexClass": "classes",
    "ApexInterface": "classes",
    "ApexEnum": "classes",
    "ApexTrigger": "triggers",
    "Flow": "flows",
    "LWC": "lwc",
}


def manifest_key_for_card(card):
    category = TYPE_TO_CATEGORY.get(card.get("type"))
    if category is None:
        return None
    if category == "lwc":
        return f"lwc/{card['id']}"
    fname = card.get("file")
    return f"{category}/{fname}" if fname else None


def assemble_context(normalized, org_index, call_graph, object_touch, file_hashes=None, recent_days=14):
    involved = set(normalized.get("involved_components", []))

    expanded = set(involved)
    for cid in list(involved):
        expanded.update(call_graph.get("calls", {}).get(cid, []))
        expanded.update(call_graph.get("called_by", {}).get(cid, []))

    primary_components = {cid: org_index[cid] for cid in expanded if cid in org_index}

    incident_objects = set()
    for s in normalized.get("soql_summary", []):
        if s.get("object"):
            incident_objects.add(s["object"])
    for d in normalized.get("dml_summary", []):
        if d.get("object"):
            incident_objects.add(d["object"])
    for cid in involved:
        card = org_index.get(cid, {})
        incident_objects.update(card.get("objects_referenced", []))
        if card.get("object"):
            incident_objects.add(card["object"])
        if card.get("start_object"):
            incident_objects.add(card["start_object"])

    related_by_object = {obj: object_touch.get(obj, {}) for obj in sorted(incident_objects)}

    recently_changed = []
    if file_hashes:
        now = datetime.datetime.utcnow()
        for cid, card in primary_components.items():
            key = manifest_key_for_card(card)
            entry = file_hashes.get(key) if key else None
            if not entry:
                continue
            try:
                last_changed = datetime.datetime.strptime(entry["last_changed"], "%Y-%m-%dT%H:%M:%SZ")
            except (KeyError, ValueError):
                continue
            age_days = (now - last_changed).total_seconds() / 86400
            card["last_changed"] = entry["last_changed"]
            card["first_seen"] = entry.get("first_seen")
            if age_days <= recent_days:
                recently_changed.append({"id": cid, "last_changed": entry["last_changed"], "age_days": round(age_days, 1)})
    recently_changed.sort(key=lambda x: x["age_days"])

    return {
        "normalized_log": normalized,
        "primary_components": primary_components,
        "related_by_object": related_by_object,
        "recently_changed_components": recently_changed,
        "notes": (
            "primary_components = components named in the log plus one call-graph "
            "hop in each direction. related_by_object lists every OTHER "
            "class/trigger/flow in the org that also reads or writes the same "
            "object(s) touched during this transaction -- check these first if "
            "the root cause isn't inside primary_components (e.g. a second "
            "trigger on the same object racing this one). recently_changed_components "
            f"lists any primary component modified within the last {recent_days} days "
            "(relative to when this context was assembled) -- a strong lead if the "
            "behavior is new."
        ),
    }


def main():
    args = sys.argv[1:]
    hashes_path = None
    recent_days = 14
    if "--hashes" in args:
        i = args.index("--hashes")
        hashes_path = args[i + 1]
        del args[i:i + 2]
    if "--recent-days" in args:
        i = args.index("--recent-days")
        recent_days = int(args[i + 1])
        del args[i:i + 2]
    log_path, cards_dir, out_dir = args[0:3]

    normalized = json.load(open(log_path))
    org_index = json.load(open(os.path.join(cards_dir, "org_index.json")))
    call_graph = json.load(open(os.path.join(cards_dir, "call_graph.json")))
    object_touch = json.load(open(os.path.join(cards_dir, "object_touch_map.json")))
    if hashes_path is None:
        default_hashes = os.path.join(cards_dir, "file_hashes.json")
        hashes_path = default_hashes if os.path.exists(default_hashes) else None
    file_hashes = json.load(open(hashes_path)) if hashes_path else None

    context_pack = assemble_context(normalized, org_index, call_graph, object_touch, file_hashes, recent_days)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "rca_context_pack.json")
    write_json(out_path, context_pack)

    pack_size = os.path.getsize(out_path)
    index_size = os.path.getsize(os.path.join(cards_dir, "org_index.json"))
    print(f"Context pack: {pack_size:,} bytes  (~{pack_size // 4:,} tokens)")
    print(f"Full org index for comparison: {index_size:,} bytes "
          f"({index_size / max(pack_size, 1):.1f}x larger than the context pack)")
    print(f"primary_components: {len(context_pack['primary_components'])}  "
          f"related objects: {len(context_pack['related_by_object'])}  "
          f"recently changed: {len(context_pack['recently_changed_components'])}")


if __name__ == "__main__":
    main()
