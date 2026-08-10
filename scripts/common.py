"""
Shared helpers for the TS Intelligent Debug Helper extraction toolkit.
"""
import re
import os
import json
import hashlib
import datetime


def iso_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_concat(paths):
    """Hash a Lightning Web Component bundle (or any multi-file unit) as a
    single logical component: concatenate each file's bytes, in a stable
    (sorted) order, into one digest."""
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()

STANDARD_OBJECTS = {
    "Account", "Contact", "Opportunity", "OpportunityLineItem", "Lead", "Case",
    "Campaign", "CampaignMember", "Contract", "Order", "OrderItem", "Product2",
    "PricebookEntry", "Pricebook2", "User", "UserRole", "Group", "Task", "Event",
    "Attachment", "ContentDocument", "ContentDocumentLink", "ContentVersion",
    "Quote", "QuoteLineItem", "Asset", "RecordType", "Profile", "PermissionSet",
    "Territory2", "Territory2Model", "ContentNote", "EmailMessage", "Note",
}


def load_custom_objects(objects_dir):
    """Read the org's objects/ backup folder and return the authoritative
    set of custom object / custom metadata / platform event API names."""
    names = set()
    if os.path.isdir(objects_dir):
        for fn in os.listdir(objects_dir):
            if fn.endswith(".object"):
                names.add(fn[: -len(".object")])
    return names


def strip_comments(code):
    """Best-effort strip of // line comments and /* */ block comments so
    they don't pollute regex matches. Not a full lexer - doesn't handle
    comment markers inside string literals, which is rare in Apex source."""
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    return code


def find_objects_referenced(code, known_objects):
    found = set()
    for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*__(?:c|mdt|e|b))\b", code):
        found.add(m.group(1))
    for obj in STANDARD_OBJECTS:
        if re.search(r"\b" + re.escape(obj) + r"\b", code):
            found.add(obj)
    # keep only ones that are real objects in this org (avoids false
    # positives like a variable literally named FooBar__c that doesn't exist)
    if known_objects:
        found = {o for o in found if o in known_objects or o in STANDARD_OBJECTS}
    return sorted(found)


def truncate(s, n=180):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
