"""
LWC extraction operating on in-memory file contents (from
LightningComponentResource.Source via the Tooling API) instead of a
lwc/<name>/ folder on disk.
"""
import re
import xml.etree.ElementTree as ET

APEX_IMPORT_RE = re.compile(r"import\s+(\w+)\s+from\s+['\"]@salesforce/apex/([\w.]+)['\"]")
WIRE_RE = re.compile(r"@wire\s*\(\s*([\w.]+)")
CHILD_TAG_RE = re.compile(r"<([a-z][a-z0-9]*-[a-z0-9-]+)\b")


def parse_lwc(name, files):
    """files: {filename: text_content} for every resource in this bundle,
    e.g. {'field.js': '...', 'field.html': '...', 'field.js-meta.xml': '...'}"""
    card = {
        "id": name, "type": "LWC", "apex_methods_imported": [], "wire_adapters": [],
        "child_components": [], "exposed_targets": [], "is_exposed": False,
    }
    js = next((c for fn, c in files.items() if fn == f"{name}.js"), None)
    html = next((c for fn, c in files.items() if fn == f"{name}.html"), None)
    meta = next((c for fn, c in files.items() if fn.endswith(".js-meta.xml")), None)

    if js:
        card["apex_methods_imported"] = sorted({m.group(2) for m in APEX_IMPORT_RE.finditer(js)})
        card["wire_adapters"] = sorted({m.group(1) for m in WIRE_RE.finditer(js)})
    if html:
        card["child_components"] = sorted({m.group(1) for m in CHILD_TAG_RE.finditer(html)})
    if meta:
        try:
            root = ET.fromstring(meta)
            for el in root.iter():
                tag = el.tag.split("}", 1)[-1]
                if tag == "isExposed" and el.text and el.text.strip().lower() == "true":
                    card["is_exposed"] = True
                if tag == "target":
                    card["exposed_targets"].append(el.text)
        except ET.ParseError:
            pass
    return card
