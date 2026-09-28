"""
The standalone normalized-log library: who owns a stored log, which customer
org / account it belongs to, who may see it, and who may archive or delete it.

A stored log is still org-independent in the sense that matters -- it was
normalized without any org's code or metadata -- but in practice every log
comes *from* some customer, and a library of forty logs named
"Non-working 1" is unsearchable. So each log can carry two optional tags:

    meta["org_id"]   -- a connected org this log came from
    meta["account"]  -- the customer account (app/accounts.py) it belongs to

plus the bookkeeping this module adds:

    meta["owner"]        -- username that stored it (absent on older logs)
    meta["archived"]     -- hidden from the default list, kept on disk
    meta["archived_at"] / meta["archived_by"]

Account follows the org
-----------------------
When a log is tagged to an org, its *effective* account is the org's current
account, read on every list -- so moving the org to another account (or
renaming the account) carries its logs along without rewriting them. The
account stored on the log is only a fallback for when the org has no account
or has since been deleted. A log with no org tag uses its stored account.

Visibility
----------
* Untagged, or tagged to an account only: visible to every signed-in user,
  exactly as every stored log was before tagging existed.
* Tagged to an org: inherits that org's visibility (app/org_access.py) --
  visible if you can see the org, if you stored the log, or if you are an
  admin. Otherwise the log is left out of listings and fetched as 404, so a
  private org's id never leaks through the log library.
* Tagged to an org that no longer exists in the registry: treated like an
  account-only log (nothing left to protect).

Managing
--------
Archive, unarchive, retag, relabel and delete are owner-or-admin. Logs stored
before ownership was tracked have no owner and can be managed by admins only
-- the same default-closed rule as pre-ownership orgs.
"""
import re

from fastapi import HTTPException

from . import accounts as accounts_mod
from . import org_access
from . import storage
from .common_now import iso_now

LOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
MAX_LABEL_LEN = 120
STATUSES = ("active", "archived", "all")


# ---------- ids / labels ----------

def check_log_id(log_id):
    """Every log id becomes a directory name. The ids this app mints are
    timestamp + slug; anything else (a '..', a separator) is refused before it
    gets near a path join."""
    if not isinstance(log_id, str) or not LOG_ID_RE.match(log_id):
        raise HTTPException(404, f"No stored normalized log '{log_id}'.")
    return log_id


def normalize_label(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    if not value:
        return None
    if len(value) > MAX_LABEL_LEN:
        raise ValueError(f"label must be at most {MAX_LABEL_LEN} characters")
    return value


# ---------- tags ----------

def _known_accounts(registry, ident):
    """Account spellings already in use -- visible orgs first, then logs --
    so a log tagged 'ibm' joins the existing 'IBM' group."""
    names = []
    for entry in registry.values():
        if org_access.can_view(entry, ident) and accounts_mod.account_of(entry):
            names.append(entry["account"])
    for m in storage.list_normalized_logs():
        if m.get("account"):
            names.append(m["account"])
    return names


def _snap(account, registry, ident):
    if not account:
        return None
    key = accounts_mod.account_key(account)
    for name in _known_accounts(registry, ident):
        if accounts_mod.account_key(name) == key:
            return name
    return account


def resolve_tags(org_id, account, ident, registry=None, validate_org=True):
    """Validate the tags a caller wants on a log. Returns (org_id, account).

    * org_id must be an org the caller can see (404 otherwise, same wording
      as every other org route, so it confirms nothing). `validate_org=False`
      is for an org tag the log already carries and the caller is not
      changing -- the owner of a log keeps it even if that org has since gone
      private on them.
    * When the org already has an account, that account wins -- a log cannot
      claim an org and a different customer at the same time.
    """
    registry = registry if registry is not None else storage.load_registry()
    org_id = (str(org_id).strip() if org_id is not None else "") or None
    try:
        account = accounts_mod.normalize_account(account)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if org_id:
        entry = org_access.assert_can_view(org_id, ident, registry) if validate_org else registry.get(org_id)
        org_account = accounts_mod.account_of(entry)
        if org_account:
            account = org_account
    return org_id, _snap(account, registry, ident)


def effective_account(meta, registry):
    org_id = meta.get("org_id")
    if org_id and org_id in registry:
        return accounts_mod.account_of(registry[org_id]) or meta.get("account")
    return meta.get("account")


# ---------- permissions ----------

def can_view(meta, ident, registry):
    if ident.get("role") == "admin":
        return True
    if meta.get("owner") and meta.get("owner") == ident.get("username"):
        return True
    org_id = meta.get("org_id")
    if org_id and org_id in registry:
        return org_access.can_view(registry[org_id], ident)
    return True


def can_manage(meta, ident):
    if ident.get("role") == "admin":
        return True
    return bool(meta.get("owner")) and meta.get("owner") == ident.get("username")


def decorate(meta, ident, registry):
    """What a caller gets back for one log: the stored meta plus the fields
    derived at read time."""
    out = dict(meta)
    out["owner"] = meta.get("owner")
    out["org_id"] = meta.get("org_id")
    out["account"] = effective_account(meta, registry)
    out["archived"] = bool(meta.get("archived"))
    out["can_manage"] = can_manage(meta, ident)
    org = registry.get(meta.get("org_id") or "")
    out["org_name"] = org.get("name") if org else None
    out["org_environment"] = accounts_mod.environment(org.get("instance_url")) if org else None
    return out


# ---------- reads ----------

def _haystack(m):
    parts = [m.get("label"), m.get("log_id"), m.get("source_log"), m.get("org_id"), m.get("org_name"),
             m.get("account"), m.get("owner"), m.get("top_exception")]
    parts.extend(m.get("involved_components") or [])
    return " ".join(str(p) for p in parts if p).casefold()


def list_logs(ident, q=None, org_id=None, account=None, owner=None, status="active", registry=None):
    """Logs the caller can see, newest first, filtered.

    q        -- case-insensitive substring over label, id, source file, org,
                account, owner, top exception and involved components. Every
                whitespace-separated term must match (AND).
    org_id   -- exact org tag.
    account  -- account name (case/whitespace-insensitive). "__unassigned__"
                selects logs with no account.
    owner    -- exact username; "me" means the caller.
    status   -- "active" (default), "archived" or "all".
    """
    if status not in STATUSES:
        raise HTTPException(400, f"status must be one of {list(STATUSES)}")
    registry = registry if registry is not None else storage.load_registry()
    terms = [t.casefold() for t in (q or "").split() if t]
    acct_key = None
    if account is not None and account != "":
        acct_key = accounts_mod.UNASSIGNED_KEY if account == accounts_mod.UNASSIGNED_KEY \
            else accounts_mod.account_key(account)
    if owner == "me":
        owner = ident.get("username")

    out = []
    for meta in storage.list_normalized_logs():
        if not can_view(meta, ident, registry):
            continue
        m = decorate(meta, ident, registry)
        if status == "active" and m["archived"]:
            continue
        if status == "archived" and not m["archived"]:
            continue
        if org_id and m.get("org_id") != org_id:
            continue
        if acct_key is not None and accounts_mod.account_key(m.get("account")) != acct_key:
            continue
        if owner and m.get("owner") != owner:
            continue
        if terms:
            hay = _haystack(m)
            if not all(t in hay for t in terms):
                continue
        out.append(m)
    return out


def get_log(log_id, ident, registry=None):
    check_log_id(log_id)
    registry = registry if registry is not None else storage.load_registry()
    result = storage.load_normalized_log(log_id)
    if not result or not result.get("meta") or not can_view(result["meta"], ident, registry):
        raise HTTPException(404, f"No stored normalized log '{log_id}'.")
    result["meta"] = decorate(result["meta"], ident, registry)
    return result


# ---------- writes ----------

def _assert_manage(log_id, ident, registry):
    """404 if the caller cannot see it, 403 with the reason if they can see
    it but did not store it."""
    check_log_id(log_id)
    meta = storage.read_log_meta(log_id)
    if not meta or not can_view(meta, ident, registry):
        raise HTTPException(404, f"No stored normalized log '{log_id}'.")
    if not can_manage(meta, ident):
        who = f"'{meta['owner']}'" if meta.get("owner") else "nobody (it was stored before owners were tracked)"
        raise HTTPException(
            403, f"Log '{log_id}' is owned by {who}. Only its owner or an admin can archive, "
                 f"retag or delete it.")
    return meta


_UNSET = object()


def update_log(log_id, ident, *, label=_UNSET, org_id=_UNSET, account=_UNSET, archived=_UNSET):
    """Owner/admin. Any argument left unset is left alone; org_id/account set
    to None clears that tag."""
    registry = storage.load_registry()
    _assert_manage(log_id, ident, registry)

    new_tags = None
    if org_id is not _UNSET or account is not _UNSET:
        current = storage.read_log_meta(log_id) or {}
        want_org = current.get("org_id") if org_id is _UNSET else org_id
        want_acct = current.get("account") if account is _UNSET else account
        # Clearing the org while keeping the old org-derived account is what
        # a person means by "untag the org"; they clear the account too if
        # they want that gone.
        new_tags = resolve_tags(want_org, want_acct, ident, registry,
                                validate_org=(org_id is not _UNSET and org_id != current.get("org_id")))
    if label is not _UNSET:
        try:
            label = normalize_label(label)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _apply(meta):
        if label is not _UNSET:
            if label:
                meta["label"] = label
            else:
                meta.pop("label", None)
        if new_tags is not None:
            for k, v in (("org_id", new_tags[0]), ("account", new_tags[1])):
                if v:
                    meta[k] = v
                else:
                    meta.pop(k, None)
        if archived is not _UNSET:
            if archived:
                if not meta.get("archived"):
                    meta["archived"] = True
                    meta["archived_at"] = iso_now()
                    meta["archived_by"] = ident.get("username")
            else:
                for k in ("archived", "archived_at", "archived_by"):
                    meta.pop(k, None)
        meta["updated_at"] = iso_now()
        meta["updated_by"] = ident.get("username")

    meta = storage.mutate_log_meta(log_id, _apply)
    return decorate(meta, ident, storage.load_registry())


def delete_log(log_id, ident):
    """Owner/admin. Removes the log's directory -- its normalized JSON and
    meta. There is no raw log to remove; none was ever stored."""
    registry = storage.load_registry()
    _assert_manage(log_id, ident, registry)
    storage.delete_normalized_log(log_id)
    return {"log_id": log_id, "deleted": True}


def facets(ident, registry=None):
    """Accounts, orgs and owners across the logs the caller can see, with
    counts -- the options for the filter dropdowns."""
    registry = registry if registry is not None else storage.load_registry()
    logs = list_logs(ident, status="all", registry=registry)
    acc, orgs, owners = {}, {}, {}
    for m in logs:
        a = m.get("account")
        k = accounts_mod.account_key(a)
        acc.setdefault(k, {"account": a, "count": 0})["count"] += 1
        if m.get("org_id"):
            orgs.setdefault(m["org_id"], {"org_id": m["org_id"], "org_name": m.get("org_name"),
                                          "account": a, "count": 0})["count"] += 1
        if m.get("owner"):
            owners[m["owner"]] = owners.get(m["owner"], 0) + 1
    return {
        "accounts": sorted(acc.values(), key=lambda g: (g["account"] is None, (g["account"] or "").casefold())),
        "orgs": sorted(orgs.values(), key=lambda o: o["org_id"].casefold()),
        "owners": [{"owner": o, "count": c} for o, c in sorted(owners.items())],
        "total": len(logs),
        "archived": sum(1 for m in logs if m["archived"]),
    }
