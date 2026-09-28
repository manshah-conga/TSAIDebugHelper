"""
Per-org visibility: who owns a connected org and who is allowed to see it.

Model
-----
Every registry entry carries two extra fields:

    "owner"      -- username of the account that connected the org
    "visibility" -- "private" (default) or "public"

Rules (agreed with the product owner):

* **private** -- visible only to its owner, plus any admin. Admins can see
  everything, consistent with them already administering users and tokens.
* **public**  -- visible to every authenticated account, at whatever role
  that account already has (a reader still only reads).
* **Managing** an org -- changing its visibility, or re-connecting /
  refreshing it -- is restricted to the owner and admins, even when the org
  is public. Public means "everyone can look", not "everyone can rewrite".
* Filing incidents and recording resolutions against an org you can *see*
  is allowed at the normal `user` role. That is the point of making an org
  public: colleagues can investigate against it without owning it.

Orgs with no visibility field
-----------------------------
Orgs connected before this feature existed have neither field. They are
treated as ``owner=None, visibility="private"``, which -- because the owner
comparison below requires a non-null owner -- makes them **visible to
admins only**.

That is a deliberate reversal. They used to default to *public* so that
nothing disappeared from an existing install, which was the right call while
every account was created by an admin. Once anyone on the VPN can register
themselves, "public" means a self-registered stranger inherits sight of
every org connected before ownership was tracked, and those are exactly the
orgs nobody has reviewed. Defaulting closed puts the decision back in an
admin's hands: they can see these orgs, and setting a visibility on one
stamps them as its owner and makes the choice explicit.

An ownerless org can only be managed by an admin, who can also claim it by
setting its visibility (which stamps an owner). Newly connected orgs default
to **private**.

Existence hiding
----------------
A private org that isn't yours is reported as 404 "not found", not 403.
Returning 403 would confirm that an org with that id exists and who might
own it; 404 leaks nothing.
"""
from fastapi import HTTPException

from . import storage

PUBLIC = "public"
PRIVATE = "private"
VISIBILITIES = (PUBLIC, PRIVATE)
DEFAULT_VISIBILITY = PRIVATE


def normalize_visibility(value, default=DEFAULT_VISIBILITY):
    """Accept the string form used by the API/UI; anything unknown is an
    error rather than a silent fallback to the permissive value."""
    if value is None or value == "":
        return default
    value = str(value).strip().lower()
    if value not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {list(VISIBILITIES)}")
    return value


def owner_of(entry):
    return (entry or {}).get("owner")


def visibility_of(entry):
    """Entries written before this feature have no visibility field. They
    read as PRIVATE, and since they also have no owner, that means
    admin-only -- see "Orgs with no visibility field" above for why this
    defaults closed rather than open."""
    v = (entry or {}).get("visibility")
    return v if v in VISIBILITIES else PRIVATE


def can_view(entry, ident):
    if visibility_of(entry) == PUBLIC:
        return True
    if ident.get("role") == "admin":
        return True
    return owner_of(entry) is not None and owner_of(entry) == ident.get("username")


def can_manage(entry, ident):
    """Change visibility, or re-connect/refresh the org."""
    if ident.get("role") == "admin":
        return True
    return owner_of(entry) is not None and owner_of(entry) == ident.get("username")


def get_entry(org_id, registry=None):
    registry = registry if registry is not None else storage.load_registry()
    return registry.get(org_id)


def visible_orgs(ident, registry=None):
    """The registry, filtered to what this identity is allowed to see, with
    `can_manage` decorated on each entry for the UI."""
    from . import accounts   # local: accounts imports this module

    registry = registry if registry is not None else storage.load_registry()
    out = {}
    for org_id, entry in registry.items():
        if not can_view(entry, ident):
            continue
        decorated = dict(entry)
        decorated["visibility"] = visibility_of(entry)
        decorated["owner"] = owner_of(entry)
        decorated["can_manage"] = can_manage(entry, ident)
        # Account grouping (app/accounts.py). `environment` and `my_domain`
        # are derived from the instance URL on every read, never stored.
        decorated["account"] = accounts.account_of(entry)
        decorated["environment"] = accounts.environment(entry.get("instance_url"))
        decorated["my_domain"] = accounts.my_domain(entry.get("instance_url"))
        out[org_id] = decorated
    return out


def assert_can_view(org_id, ident, registry=None):
    """Returns the registry entry, or raises 404. A private org you are not
    allowed to see is indistinguishable from one that does not exist."""
    entry = get_entry(org_id, registry)
    if entry is None or not can_view(entry, ident):
        raise HTTPException(404, f"No org '{org_id}' (or you do not have access to it).")
    return entry


def assert_can_manage(org_id, ident, registry=None):
    """Returns the registry entry, or raises. 404 if you cannot even see it;
    403 (with an explanation) if you can see it but do not own it."""
    entry = assert_can_view(org_id, ident, registry)
    if not can_manage(entry, ident):
        owner = owner_of(entry)
        who = f"'{owner}'" if owner else "nobody yet (it predates org ownership)"
        raise HTTPException(
            403,
            f"Org '{org_id}' is owned by {who}. Only its owner or an admin can "
            f"change its visibility or re-connect it."
            + ("" if owner else " An admin can claim it by setting its visibility."),
        )
    return entry


def set_visibility(org_id, visibility, ident):
    """Owner/admin-only. Stamps the acting user as owner if the org has none
    (an admin adopting a legacy, pre-ownership org)."""
    visibility = normalize_visibility(visibility, default=None)
    if visibility is None:
        raise ValueError("visibility is required")

    # The permission check and the write happen inside one lock over the
    # registry. Splitting them let an org fetch finishing at the same moment
    # rewrite the whole registry from its own snapshot and drop this
    # visibility change -- an org silently staying private after the owner
    # was told it went public is a confidentiality-shaped bug, not a cosmetic
    # one.
    result = {}

    def _apply(registry):
        entry = assert_can_manage(org_id, ident, registry)
        entry["visibility"] = visibility
        if not entry.get("owner"):
            entry["owner"] = ident["username"]
        registry[org_id] = entry
        result.update({"org_id": org_id, "visibility": visibility, "owner": entry["owner"]})

    storage.mutate_registry(_apply)
    return result
