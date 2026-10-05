"""
Component-id resolution and size-aware card shaping for the lookup routes.

Two failures seen in chat on 2026-10-05 (org 00DDL00000CU2dS, gpt-6.1-sol):

1. `get_inbound_references("Apttus_CPQApi.CPQWebService")` returned nothing,
   although the index held 20 callers -- under the key `CPQWebService`,
   because a managed stub's KB id is the bare class name. Models (and people)
   naturally write the namespace-qualified name, often with the method on the
   end. `resolve()` accepts all of those.

2. `get_component("APTS_CustomSolutionctrl")` was 52 KB, and the chat layer's
   blind character cut at 24 KB fell in the middle of `soql`. The section that
   held the answer -- `calls_to` -- came after it and was never seen. Cutting a
   JSON string at an arbitrary byte is the wrong tool: `shape()` instead keeps
   the small, high-signal sections whole, trims the bulky lists, and says
   exactly which sections were trimmed and how to fetch them (`sections=` or
   `method=`), so the model can ask for what it actually needs.
"""
import json

# Always returned, so a filtered card is still identifiable.
IDENTITY_KEYS = ("id", "type", "schema_version", "extractor_version", "extracted_at",
                 "source_api_version", "namespace", "is_managed", "is_customer_authored",
                 "source_first_seen", "source_last_changed",
                 "loc", "sharing", "is_test_class", "extends", "implements", "object",
                 "events", "label", "status", "mechanism", "file")

# Digest order: what answers the most questions per byte goes first. Keys not
# listed sit between these and the bulky tail, so a flow's `elements` (its main
# content) is not pushed behind Apex-specific lists.
PRIORITY = ("entry_points", "flags", "calls_to", "call_sites", "async_dispatches", "callouts",
            "named_credentials", "dml", "exceptions_thrown", "exceptions_caught",
            "custom_exceptions_defined", "static_mutable_state", "objects_referenced",
            "methods")
BULKY_TAIL = ("soql", "field_writes")

# A list item belongs to a method when one of these keys names it.
_METHOD_KEYS = ("method", "caller_method", "name")


def case_variants(index, cid):
    """Every key spelled like `cid` ignoring case. Apex names are
    case-insensitive, so `CPQWebService` and `CPQWebservice` written by two
    different callers are the same class -- and the inbound index, keyed by
    the text each caller wrote, holds both."""
    low = (cid or "").lower()
    return [k for k in index if k.lower() == low]


def resolve(index, raw, cards=None):
    """(component_id, method_filter, note) for a requested id, or (None, None, None).

    Accepts the exact id, any casing of it, and namespace/method-qualified
    forms: `Ns.Class`, `Ns.Class.method`, `Class.method`. A namespace given
    must match the card's (when the card has one), so `Foo.CPQWebService`
    cannot silently resolve to Apttus_CPQApi's class. `cards` is where that
    namespace is read from when `index` is not itself the card index (the
    inbound index carries no namespaces).
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None, None
    cards = index if cards is None else cards
    lower_cards = {k.lower(): v for k, v in cards.items()}
    if raw in index:
        return raw, None, None
    lower = {k.lower(): k for k in index}
    if raw.lower() in lower:
        cid = lower[raw.lower()]
        return cid, None, f"resolved '{raw}' to '{cid}' (case-insensitive)"

    parts = [p for p in raw.split(".") if p]
    for i, part in enumerate(parts):
        cid = part if part in index else lower.get(part.lower())
        if not cid:
            continue
        ns = parts[i - 1] if i > 0 else None
        card = lower_cards.get(cid.lower())
        card_ns = card.get("namespace") if isinstance(card, dict) else None
        if ns and card_ns and ns.lower() != card_ns.lower():
            continue
        if ns and not card_ns and isinstance(card, dict) and card.get("is_managed") is False:
            continue                       # `Ns.X` cannot be the org's own un-namespaced X
        method = parts[i + 1] if i + 1 < len(parts) else None
        note = f"resolved '{raw}' to component '{cid}'"
        if ns:
            note += f" (namespace {card_ns or ns})"
        if method:
            note += f", filtered to method '{method}'"
        return cid, method, note
    return None, None, None


def _item_matches_method(item, method):
    m = method.lower()
    if not isinstance(item, dict):
        return False
    for k in _METHOD_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.lower() == m:
            return True
    if (item.get("method_called") or "").lower() == m:
        return True
    detail = item.get("detail")
    if isinstance(detail, str) and detail.lower().startswith(m + "("):
        return True
    called = item.get("methods_called")
    return isinstance(called, list) and any((x or "").lower() == m for x in called)


def filter_method(card, method):
    """Keep only list entries that belong to (or call) `method`."""
    m = method.lower()
    out = {}
    for k, v in card.items():
        if k == "call_sites" and isinstance(v, list):
            # Its own outbound calls, in order -- and, separately, where in
            # this class it is itself called from, so order can be traced
            # upward without mixing the two.
            out[k] = [x for x in v if isinstance(x, dict)
                      and (x.get("caller_method") or "").lower() == m]
            callers = [x for x in v if isinstance(x, dict) and x.get("kind") == "same_class"
                       and (x.get("method_called") or "").lower() == m]
            if callers:
                out["_called_from_in_class"] = callers
            continue
        if k in IDENTITY_KEYS or not isinstance(v, list):
            out[k] = v
            continue
        if v and all(isinstance(x, dict) for x in v):
            out[k] = [x for x in v if _item_matches_method(x, method)]
        else:
            out[k] = v
    out["_filter"] = {"method": method,
                      "note": "List sections keep only entries in, or calling, this method. "
                              "call_sites are this method's own outbound calls in source "
                              "order, with the conditions around each; _called_from_in_class "
                              "lists where this class calls it."}
    return out


def select_sections(card, sections):
    wanted = {s.strip() for s in sections if s and s.strip()}
    out = {k: v for k, v in card.items() if k in IDENTITY_KEYS or k in wanted}
    missing = sorted(wanted - set(card))
    if missing:
        out["_missing_sections"] = missing
    return out


def _size(v):
    return len(json.dumps(v, default=str))


def shape(card, max_chars):
    """Fit a card into `max_chars` of JSON without cutting mid-structure."""
    if not max_chars or _size(card) <= max_chars:
        return card

    keys = list(card.keys())
    order = ([k for k in keys if k in IDENTITY_KEYS]
             + [k for k in PRIORITY if k in card]
             + [k for k in keys if k not in IDENTITY_KEYS and k not in PRIORITY
                and k not in BULKY_TAIL]
             + [k for k in BULKY_TAIL if k in card])

    budget = max_chars - 900          # room for the _truncated note itself
    out, trimmed = {}, {}
    for k in order:
        v = card[k]
        cost = _size(v) + len(k) + 6
        if cost <= budget:
            out[k] = v
            budget -= cost
            continue
        if isinstance(v, list):
            kept, used = [], 2
            for item in v:
                c = _size(item) + 2
                if used + c > budget:
                    break
                kept.append(item)
                used += c
            out[k] = kept
            budget -= used + len(k) + 6
            trimmed[k] = {"total": len(v), "shown": len(kept)}
        else:
            trimmed[k] = {"omitted": True, "chars": cost}
        if budget < 0:
            budget = 0

    out["_truncated"] = {
        "reason": f"TRUNCATED: this result is {_size(card)} chars; trimmed to fit {max_chars}. "
                  f"Tell the user if the answer depends on a trimmed section.",
        "sections": trimmed,
        "how_to_get_the_rest": "Call get_component again with sections=[...] naming the trimmed "
                               "sections, or method='<name>' to keep only one method's entries. "
                               "Do not conclude something is absent from a trimmed section.",
    }
    return out
