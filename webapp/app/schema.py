"""
Shared knowledgebase-schema constants and helpers (schema v3).

Implements the cross-card pieces of the v3 proposal: the card envelope
(§4.1), namespace / manageability (§4.3), and the unified value model
(§8). Everything here is small and pure so every extractor can reuse it
without importing from another extractor (design principle P1: extraction
stays per-file and independent).
"""

# Bump SCHEMA_VERSION on any breaking change to card shape; bump
# EXTRACTOR_VERSION on any parser behaviour change. Consumers should refuse
# to reason about a card whose schema_version exceeds what they understand.
SCHEMA_VERSION = 3
EXTRACTOR_VERSION = "3.1.0"

# $-prefixed references that are global values rather than the triggering record.
_GLOBAL_PREFIXES = ("$User", "$Organization", "$Profile", "$System", "$Api",
                    "$Label", "$Setup", "$Permission", "$Flow", "$Client")

# Conga PS authored components use this prefix inside an unmanaged namespace.
CONGA_PS_PREFIX = "CNG_"


def envelope(source_api_version=None):
    """The common envelope stamped on every card (§4.1). `extracted_at` is
    filled by the caller via iso_now() to avoid importing time here."""
    from .common_now import iso_now
    return {
        "schema_version": SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": iso_now(),
        "source_api_version": source_api_version,
    }


def namespace_fields(namespace_prefix, dev_name=None):
    """§4.3 -- manageability of a component. `namespace_prefix` is the
    Salesforce NamespacePrefix (None/'' for unmanaged, e.g. 'Apttus' for a
    managed-package component). Also flags the Conga-PS `CNG_` convention as
    a soft signal (not a substitute for the manageability flag)."""
    ns = namespace_prefix or None
    is_managed = bool(ns)
    out = {
        "namespace": ns,
        "is_managed": is_managed,
        "is_customer_authored": not is_managed,
    }
    if dev_name and str(dev_name).startswith(CONGA_PS_PREFIX):
        out["is_conga_ps_authored"] = True
    return out


# ---------- unified value model (§8) ----------

def value_literal(vtype, value, **extra):
    v = {"kind": "literal", "type": vtype, "value": value}
    v.update(extra)
    return v


def value_reference(ref):
    if isinstance(ref, str) and ref.startswith(_GLOBAL_PREFIXES):
        return {"kind": "global", "type": "unknown", "value": ref}
    return {"kind": "reference", "type": "unknown", "value": ref}


def value_unparsed(raw, reason):
    return {"kind": "unparsed", "raw": raw, "reason": reason}


# Scalar members of the Flow value union, checked in order. CRITICAL: the
# live Tooling API JSON includes EVERY union member as a key, with the unused
# ones set to null -- so we must branch on the first member whose value is
# not None, never on mere key presence (that v3.0.0 bug made every value read
# as boolean-null). Reference/formula/sobject members are checked first so a
# reference wins over a co-present null scalar.
_SCALAR_MEMBERS = [
    ("booleanValue", "boolean"),
    ("numberValue", "number"),
    ("dateTimeValue", "datetime"),
    ("dateValue", "date"),
    ("stringValue", "string"),
]
_KNOWN_MEMBERS = {m for m, _ in _SCALAR_MEMBERS} | {
    "elementReference", "apexValue", "sobjectValue", "formulaExpression", "formulaDataType"}


def flow_value(v):
    """Convert a Flow metadata value node into the unified model (§8).
    Resolves the two v2/v3.0.0 ambiguities: a checkbox is boolean not a null
    string, and a blank assignment is an explicit typed-null. Never fabricates
    a type -- an unrecognised non-null member becomes kind:'unparsed' (P3)."""
    if v is None:
        return None
    if not isinstance(v, dict):
        if isinstance(v, bool):
            return value_literal("boolean", v)
        if isinstance(v, (int, float)):
            return value_literal("number", v)
        return value_literal("string", v)

    if v.get("elementReference") is not None:
        return value_reference(v["elementReference"])
    if v.get("apexValue") is not None:
        return value_reference(v["apexValue"])
    if v.get("sobjectValue") is not None:
        return {"kind": "variable", "type": "sobject", "value": v["sobjectValue"]}
    if v.get("formulaExpression") is not None:
        ftype = (v.get("formulaDataType") or "string")
        return {"kind": "formula", "type": str(ftype).lower(), "expression": v["formulaExpression"]}

    for member, vtype in _SCALAR_MEMBERS:
        val = v.get(member)
        if val is not None:
            if member == "stringValue" and val == "":
                return value_literal("null", None, is_blank_assignment=True)
            return value_literal(vtype, val)

    # No known member carried a value. If some UNKNOWN member is non-null, we
    # genuinely couldn't parse it -- say so, don't fabricate a null. If every
    # member is null/absent, this is a deliberate blank assignment.
    extra = {k: val for k, val in v.items() if k not in _KNOWN_MEMBERS and val is not None}
    if extra:
        return value_unparsed(v, "unsupported_union_member")
    return value_literal("null", None, is_blank_assignment=True)


# value `kind`s that count as a resolved value node, for parse_stats.
_VALUE_KINDS = {"literal", "reference", "formula", "variable", "global", "unparsed"}


def count_value_nodes(obj):
    """Walk a card and count value-model nodes and how many are 'unparsed'
    (§ D-02.4 parse_stats): a non-zero unparsed rate on a re-extraction is an
    immediate signal rather than something a consumer must notice by eye."""
    total = unparsed = 0

    def walk(o):
        nonlocal total, unparsed
        if isinstance(o, dict):
            if o.get("kind") in _VALUE_KINDS:
                total += 1
                if o.get("kind") == "unparsed":
                    unparsed += 1
                return  # a value node's own fields aren't further value nodes
            for val in o.values():
                walk(val)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return {"values_total": total, "values_unparsed": unparsed}
