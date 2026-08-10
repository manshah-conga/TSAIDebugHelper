"""
extract_lwc.py — extractor for Lightning Web Component bundles.

For each lwc/<component>/ folder, produces a card with: imported Apex
methods (the most common source of a "flow works, but the UI is broken"
bug), @wire usage, child custom-element tags used in the template, and
where the component is exposed (js-meta.xml targets).

Usage:
    python extract_lwc.py <lwc_dir> <out_dir>
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

from common import write_json

APEX_IMPORT_RE = re.compile(
    r"import\s+(\w+)\s+from\s+['\"]@salesforce/apex/([\w.]+)['\"]")
WIRE_RE = re.compile(r"@wire\s*\(\s*([\w.]+)")
CHILD_TAG_RE = re.compile(r"<([a-z][a-z0-9]*-[a-z0-9-]+)\b")


def parse_lwc(name, dir_path):
    card = {
        "id": name,
        "type": "LWC",
        "apex_methods_imported": [],
        "wire_adapters": [],
        "child_components": [],
        "exposed_targets": [],
        "is_exposed": False,
    }
    files = os.listdir(dir_path)
    js_file = next((f for f in files if f == name + ".js"), None)
    html_file = next((f for f in files if f == name + ".html"), None)
    meta_file = next((f for f in files if f.endswith(".js-meta.xml")), None)

    if js_file:
        with open(os.path.join(dir_path, js_file), "r", encoding="utf-8", errors="replace") as f:
            js = f.read()
        card["apex_methods_imported"] = sorted({m.group(2) for m in APEX_IMPORT_RE.finditer(js)})
        card["wire_adapters"] = sorted({m.group(1) for m in WIRE_RE.finditer(js)})

    if html_file:
        with open(os.path.join(dir_path, html_file), "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        card["child_components"] = sorted({m.group(1) for m in CHILD_TAG_RE.finditer(html)})

    if meta_file:
        try:
            tree = ET.parse(os.path.join(dir_path, meta_file))
            root = tree.getroot()
            for el in root.iter():
                tag = el.tag.split("}", 1)[-1]
                if tag == "isExposed" and el.text and el.text.strip().lower() == "true":
                    card["is_exposed"] = True
                if tag == "target":
                    card["exposed_targets"].append(el.text)
        except Exception:
            pass

    return card


def main():
    lwc_dir, out_dir = sys.argv[1:3]
    cards = {}
    errors = []
    for name in os.listdir(lwc_dir):
        dir_path = os.path.join(lwc_dir, name)
        if not os.path.isdir(dir_path):
            continue
        try:
            cards[name] = parse_lwc(name, dir_path)
        except Exception as e:
            errors.append({"component": name, "error": str(e)})

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "lwc_cards.json"), cards)
    write_json(os.path.join(out_dir, "lwc_extract_errors.json"), errors)
    print(f"Parsed {len(cards)} LWC components, {len(errors)} errors.")


if __name__ == "__main__":
    main()
