"""
Customer accounts: grouping connected orgs under the customer they belong to.

One customer usually means several orgs -- production plus a handful of
sandboxes (UAT, dev, a full-copy for upgrade testing) -- and before this the
org list was a flat alphabetical run of ids where "ibmdev2admin" and "C2UAT"
gave no clue which customer they belonged to.

Model
-----
An account is a *label on the registry entry*, not a separate record:

    registry[org_id]["account"] = "IBM"      # absent / None = unassigned

That keeps it where every other per-org setting already lives (one document,
one lock, see storage.mutate_registry), needs no migration, and means an org
can never point at an account that has been deleted. The cost is that an
account has no attributes of its own beyond its name -- deliberate, until
there is something an account needs to carry that an org does not.

Names
-----
Account names are compared case- and whitespace-insensitively (`account_key`)
so "IBM", "ibm " and "Ibm" group together. When a name is saved it is snapped
to the spelling already in use, so the first person to type it decides how it
is displayed and later typos in capitalisation do not fork the group.

Suggestions
-----------
A Salesforce sandbox's host is ``<prod-my-domain>--<sandbox>.sandbox.my.
salesforce.com``, so the part before ``--`` is the same for a production org
and every sandbox cut from it. That is a reliable "these belong together"
signal: if an org you can see on the same My Domain already has an account,
that account is the suggestion; otherwise the My Domain name itself is.

Permissions
-----------
Setting an org's account is a management action -- owner or admin, same as
visibility (org_access.assert_can_manage). Renaming an account retags every
org in it that the caller can manage; if the caller can *see* an org in the
account that they cannot manage, the rename is refused rather than splitting
the account in two behind their back. Orgs the caller cannot see are neither
counted nor mentioned (they would leak existence) and keep the old name --
an admin rename covers everything.
"""
import re
from urllib.parse import urlparse

from fastapi import HTTPException

from . import org_access
from . import storage

MAX_ACCOUNT_LEN = 80
UNASSIGNED_KEY = "__unassigned__"

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


# ---------- names ----------

def normalize_account(value):
    """Clean a user-supplied account name. Empty -> None (unassigned)."""
    if value is None:
        return None
    value = _WS.sub(" ", str(value)).strip()
    if not value:
        return None
    if _CTRL.search(value):
        raise ValueError("account name contains control characters")
    if len(value) > MAX_ACCOUNT_LEN:
        raise ValueError(f"account name must be at most {MAX_ACCOUNT_LEN} characters")
    return value


def account_key(name):
    """Grouping key: case- and whitespace-insensitive. None -> unassigned."""
    name = normalize_account(name) if name is not None else None
    return name.casefold() if name else UNASSIGNED_KEY


def account_of(entry):
    return (entry or {}).get("account") or None


def snap_to_existing(name, registry, skip_org=None):
    """Return the spelling already used by another org for this account, or
    `name` unchanged if nobody uses it yet."""
    if not name:
        return None
    key = account_key(name)
    for oid, entry in registry.items():
        if oid == skip_org:
            continue
        existing = account_of(entry)
        if existing and account_key(existing) == key:
            return existing
    return name


# ---------- instance URL heuristics ----------

def _host(instance_url):
    url = (instance_url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def my_domain(instance_url):
    """The production My Domain name an org hangs off, e.g.
    'acme' for acme.my.salesforce.com and acme--uat.sandbox.my.salesforce.com.
    None when the host is not a My Domain host (legacy naX / csX instances)."""
    host = _host(instance_url)
    if not host:
        return None
    first = host.split(".")[0]
    if ".my.salesforce.com" not in host and ".lightning.force.com" not in host \
            and ".my.site.com" not in host and ".force.com" not in host:
        return None
    if re.fullmatch(r"(na|cs|eu|ap|um|gs)\d+", first):
        return None
    return first.split("--")[0] or None


def environment(instance_url):
    """'production' | 'sandbox' | 'developer' | 'scratch' | 'unknown'.
    Derived, never stored -- it is a pure function of the URL."""
    host = _host(instance_url)
    if not host:
        return "unknown"
    if ".scratch." in host:
        return "scratch"
    if ".develop." in host:
        return "developer"
    if ".sandbox." in host or host.startswith("test.") or re.match(r"^cs\d+\.", host) \
            or "--" in host.split(".")[0]:
        return "sandbox"
    if "salesforce.com" in host or "force.com" in host:
        return "production"
    return "unknown"


def suggest(instance_url, ident, registry=None):
    """What the Connect form should pre-fill. Only orgs the caller can see are
    consulted, so a suggestion never reveals a private org's account name."""
    registry = registry if registry is not None else storage.load_registry()
    dom = my_domain(instance_url)
    matched = None
    siblings = []
    if dom:
        for oid, entry in registry.items():
            if not org_access.can_view(entry, ident):
                continue
            if my_domain(entry.get("instance_url")) == dom:
                siblings.append(oid)
                if account_of(entry) and not matched:
                    matched = account_of(entry)
    return {
        "my_domain": dom,
        "environment": environment(instance_url),
        "matched_account": matched,
        "siblings": siblings,
        "suggestion": matched or dom,
    }


# ---------- listing ----------

def list_accounts(ident, registry=None):
    """Accounts the caller can see, with their orgs. Unassigned orgs are
    reported under account None so a caller can find what still needs
    grouping."""
    visible = org_access.visible_orgs(ident, registry)
    groups = {}
    for oid, entry in visible.items():
        name = account_of(entry)
        g = groups.setdefault(account_key(name), {
            "account": name, "orgs": [], "environments": {}, "can_manage_all": True})
        g["orgs"].append(oid)
        env = environment(entry.get("instance_url"))
        g["environments"][env] = g["environments"].get(env, 0) + 1
        g["can_manage_all"] = g["can_manage_all"] and bool(entry.get("can_manage"))
    out = sorted(groups.values(), key=lambda g: (g["account"] is None, (g["account"] or "").casefold()))
    for g in out:
        g["orgs"].sort()
        g["org_count"] = len(g["orgs"])
    return out


# ---------- writes ----------

def set_account(org_id, account, ident):
    """Owner/admin only. `account` None/'' unassigns."""
    account = normalize_account(account)
    result = {}

    def _apply(registry):
        entry = org_access.assert_can_manage(org_id, ident, registry)
        snapped = snap_to_existing(account, registry, skip_org=org_id)
        if snapped:
            entry["account"] = snapped
        else:
            entry.pop("account", None)
        registry[org_id] = entry
        result.update({"org_id": org_id, "account": snapped})

    storage.mutate_registry(_apply)
    return result


def rename_account(old, new, ident):
    """Retag every org in account `old` that the caller can manage. `new`
    may be an existing account (a merge) or None (ungroup)."""
    old_key = account_key(old)
    if old_key == UNASSIGNED_KEY:
        raise ValueError("choose an account to rename -- unassigned orgs are moved one at a time")
    new = normalize_account(new)
    result = {}

    def _apply(registry):
        members = [oid for oid, e in registry.items() if account_key(account_of(e)) == old_key]
        visible = [oid for oid in members if org_access.can_view(registry[oid], ident)]
        if not visible:
            raise HTTPException(404, f"No account '{old}' (or you do not have access to any of its orgs).")
        blocked = [oid for oid in visible if not org_access.can_manage(registry[oid], ident)]
        if blocked:
            raise HTTPException(
                403,
                f"You cannot rename '{old}': you do not manage "
                f"{len(blocked)} of its {len(visible)} org(s) ({', '.join(sorted(blocked)[:5])}"
                f"{'...' if len(blocked) > 5 else ''}). Ask their owner or an admin, or move "
                f"your own orgs to another account one at a time.")
        target = snap_to_existing(new, {k: v for k, v in registry.items() if k not in visible}) if new else None
        for oid in visible:
            if target:
                registry[oid]["account"] = target
            else:
                registry[oid].pop("account", None)
        result.update({"from": old, "to": target, "orgs": sorted(visible)})

    storage.mutate_registry(_apply)
    return result
