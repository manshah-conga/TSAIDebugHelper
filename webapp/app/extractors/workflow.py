"""
Workflow / Approval field-update extraction.

Both Workflow Rules and Approval Processes fire field updates through the
same `WorkflowFieldUpdate` metadata type, so indexing WorkflowFieldUpdate
covers both mechanisms in one pass. Each field update names exactly one
target field on one object and the value it writes (a literal or a
formula), which is precisely the declarative-writer signal that Apex-only
field-writer detection was missing.

Input is the WorkflowFieldUpdate metadata as returned by the Tooling API
(either the `Metadata` compound field from a Tooling SOQL query, or the
generic sobject GET /tooling/sobjects/WorkflowFieldUpdate/<id>). The
object is taken from the FullName prefix ("Object.Api_Name"); the field,
operation and value come from the metadata body.

Not yet verified against a live org (this project has had no real token);
every access is defensive so an unexpected shape yields a thinner card
rather than aborting the org fetch.
"""


def _describe_value(metadata):
    op = (metadata.get("operation") or "").strip()
    if metadata.get("formula"):
        return {"kind": "formula", "value": metadata["formula"]}
    if metadata.get("literalValue") is not None:
        return {"kind": "literal", "value": metadata["literalValue"]}
    if op:
        # operations like Null, NextValue, PreviousValue carry no explicit value
        return {"kind": "operation", "value": op}
    return None


def parse_workflow_field_update(full_name, metadata, namespace_prefix=None, api_version=None):
    """full_name is 'Object.FieldUpdateApiName'. Returns a component card of
    type WorkflowFieldUpdate, or None if the target field can't be
    determined."""
    from .. import schema
    metadata = metadata or {}
    obj = full_name.split(".", 1)[0] if "." in full_name else metadata.get("targetObject")
    field = metadata.get("field")
    if not field:
        return None
    # A cross-object field update writes another object via a lookup; the
    # metadata `field` may be "Lookup__r.Target__c" -- keep the raw target
    # field name (last segment) for indexing plus the full path for detail.
    target_field = field.rsplit(".", 1)[-1]
    return {
        "id": full_name,
        "type": "WorkflowFieldUpdate",
        **schema.envelope(api_version),
        **schema.namespace_fields(namespace_prefix, full_name),
        "mechanism": "Workflow/Approval field update",
        "object": obj,
        "field": target_field,
        "field_path": field,
        "operation": metadata.get("operation"),
        "value": _describe_value(metadata),
        "reevaluate_on_change": metadata.get("reevaluateOnChange"),
        "label": metadata.get("name") or metadata.get("fullName") or full_name,
        "file": None,
    }
