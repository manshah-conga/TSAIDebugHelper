"""
extract_flow.py — extractor for Salesforce Flow metadata (.flow files).

Flows are clean XML, so this uses ElementTree instead of regex. Produces
one component card per flow capturing: process type, trigger object/type,
element counts, Apex/subflow actions invoked, and fault-path coverage
(a good proxy for "does this flow handle errors at all").

Usage:
    python extract_flow.py <flows_dir> <out_dir>
"""
import os
import sys
import xml.etree.ElementTree as ET

from common import write_json

NS = "{http://soap.sforce.com/2006/04/metadata}"


def local(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def text(el, tag):
    child = el.find(NS + tag)
    return child.text if child is not None else None


def parse_flow(name, path):
    tree = ET.parse(path)
    root = tree.getroot()

    card = {
        "id": name,
        "type": "Flow",
        "label": None,
        "processType": None,
        "apiVersion": text(root, "apiVersion"),
        "start_object": None,
        "trigger_type": None,
        "record_trigger_type": None,
        "element_counts": {},
        "apex_actions_called": [],
        "subflows_called": [],
        "fault_paths": 0,
        "total_connectors": 0,
    }

    element_tags = [
        "decisions", "assignments", "recordCreates", "recordUpdates",
        "recordLookups", "recordDeletes", "actionCalls", "subflows",
        "screens", "loops", "waits", "collectionProcessors",
    ]
    counts = {}
    for child in root:
        tag = local(child.tag)
        if tag == "label":
            card["label"] = child.text
        elif tag == "processType":
            card["processType"] = child.text
        elif tag == "start":
            obj = text(child, "object")
            if obj:
                card["start_object"] = obj
            tt = text(child, "triggerType")
            if tt:
                card["trigger_type"] = tt
            rtt = text(child, "recordTriggerType")
            if rtt:
                card["record_trigger_type"] = rtt
        if tag in element_tags:
            counts[tag] = counts.get(tag, 0) + 1
        if tag == "actionCalls":
            action_type = text(child, "actionType")
            action_name = text(child, "actionName")
            if action_type and action_type.lower() == "apex" and action_name:
                card["apex_actions_called"].append(action_name)
        if tag == "subflows":
            fn = text(child, "flowName")
            if fn:
                card["subflows_called"].append(fn)

    card["element_counts"] = counts

    fault_count = 0
    connector_count = 0
    for el in root.iter():
        tag = local(el.tag)
        if tag == "faultConnector":
            fault_count += 1
        if tag == "connector":
            connector_count += 1
    card["fault_paths"] = fault_count
    card["total_connectors"] = connector_count

    return card


def main():
    flows_dir, out_dir = sys.argv[1:3]
    cards = {}
    errors = []
    for fn in os.listdir(flows_dir):
        if not fn.endswith(".flow"):
            continue
        name = os.path.splitext(fn)[0]
        path = os.path.join(flows_dir, fn)
        try:
            card = parse_flow(name, path)
            card["file"] = fn
            cards[name] = card
        except Exception as e:
            errors.append({"file": fn, "error": str(e)})

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "flow_cards.json"), cards)
    write_json(os.path.join(out_dir, "flow_extract_errors.json"), errors)
    print(f"Parsed {len(cards)} flows, {len(errors)} errors.")


if __name__ == "__main__":
    main()
