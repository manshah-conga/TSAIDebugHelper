"""
Shared parsing helpers -- identical logic to the standalone scripts'
common.py, just imported by the web app instead of run as a CLI.
"""
import re
import hashlib
import datetime

STANDARD_OBJECTS = {
    "Account", "Contact", "Opportunity", "OpportunityLineItem", "Lead", "Case",
    "Campaign", "CampaignMember", "Contract", "Order", "OrderItem", "Product2",
    "PricebookEntry", "Pricebook2", "User", "UserRole", "Group", "Task", "Event",
    "Attachment", "ContentDocument", "ContentDocumentLink", "ContentVersion",
    "Quote", "QuoteLineItem", "Asset", "RecordType", "Profile", "PermissionSet",
    "Territory2", "Territory2Model", "ContentNote", "EmailMessage", "Note",
}


def iso_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_comments(code):
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
    if known_objects:
        found = {o for o in found if o in known_objects or o in STANDARD_OBJECTS}
    return sorted(found)


def truncate(s, n=180):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."
