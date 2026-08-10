"""
RCA context assembly + field-writer lookup, operating on the in-memory
knowledgebase dicts for a single org (org_index, call_graph,
object_touch_map, field_touch_map) instead of reading them from files.
Same logic as the standalone build_rca_context.py / find_field_writers.py
scripts.
"""
import datetime

TYPE_TO_CATEGORY = {
    "ApexClass": "classes", "ApexInterface": "classes", "ApexEnum": "classes",
    "ApexTrigger": "triggers", "Flow": "flows", "LWC": "lwc",
}


def _manifest_key_for_card(card):
    if card.get("type") == "WorkflowFieldUpdate":
        return f"workflow/{card['id']}"
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
            key = _manifest_key_for_card(card)
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
            "primary_components = components named in the log plus one call-graph hop in each "
            "direction. related_by_object lists every OTHER class/trigger/flow in the org that "
            "also reads or writes the same object(s) touched during this transaction. "
            f"recently_changed_components lists any primary component modified within the last "
            f"{recent_days} days."
        ),
    }


def lookup_field_writers(field, org_index, field_touch_map, file_hashes=None):
    """Answers 'field X had the wrong value, no exception' using only the
    knowledgebase: every writer of `field`, ranked by risk, with recency."""
    now = datetime.datetime.utcnow()
    writers = field_touch_map.get(field, [])
    enriched = []
    for w in writers:
        card = org_index.get(w["component"], {})
        key = _manifest_key_for_card(card)
        last_changed = (file_hashes or {}).get(key, {}).get("last_changed") if key else None
        age_days = None
        if last_changed:
            age_days = round((now - datetime.datetime.strptime(last_changed, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 86400, 1)
        enriched.append({
            **w, "card_type": card.get("type"), "card_file": card.get("file"),
            "last_changed": last_changed, "age_days": age_days,
        })
    return {"field": field, "writers": enriched}
