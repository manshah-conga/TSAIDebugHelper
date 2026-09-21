"""
Chat persistence and share links.

Layout mirrors the rest of the app -- plain JSON under data/, written through
storage.write_json so every disk write in the process stays in one auditable
place:

    data/chats/{username}/{chat_id}/meta.json      title, org, model, totals
    data/chats/{username}/{chat_id}/messages.json  the full turn list
    data/chats/_shares.json                        share token -> chat pointer

Ownership is implicit in the path, which is deliberate: it closes by
construction the gap that /api/logs* still has, where every stored item is
readable by every reader.

Sharing
-------
A share is an unguessable token that maps to one (username, chat_id). Opening
a shared transcript needs no account -- that is the point, since the audience
is usually a customer or a colleague outside this app -- so the rendered view
is strictly read-only and the payload is filtered by `redact_for_share`:

  * tool ARGUMENTS and RESULTS are dropped unless the sharer opts in. A
    tool result carries org internals (component cards, field maps, incident
    packs) that the recipient may have no right to see, and the org's own
    visibility rules cannot be evaluated for an anonymous viewer.
  * the org id is shown only as a label; nothing in a shared page links back
    into an authenticated route.

Revoking a share deletes the token. There is no soft delete: the link stops
working immediately for everyone.
"""
import datetime
import glob
import os
import secrets

from . import storage

CHATS_ROOT = os.path.join(storage.DATA_ROOT, "chats")
SHARES_PATH = os.path.join(CHATS_ROOT, "_shares.json")

MAX_TITLE = 80


def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_dir(username):
    return os.path.join(CHATS_ROOT, username)


def _chat_dir(username, chat_id):
    return os.path.join(_user_dir(username), chat_id)


def new_chat_id():
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{secrets.token_hex(3)}"


def _safe_id(chat_id):
    """A chat id comes off the URL, so it must never be able to walk out of
    the user's own directory."""
    cid = str(chat_id or "")
    if not cid or "/" in cid or "\\" in cid or cid.startswith("."):
        return None
    return cid


# ---------- CRUD ----------

def create_chat(username, org_id=None, title=None, model=None):
    chat_id = new_chat_id()
    meta = {
        "chat_id": chat_id,
        "owner": username,
        "title": (title or "New conversation")[:MAX_TITLE],
        "org_id": org_id,
        "model": model,
        "created_at": _now(),
        "updated_at": _now(),
        "message_count": 0,
        "tool_call_count": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "share_token": None,
        "share_includes_tools": False,
    }
    storage.write_json(os.path.join(_chat_dir(username, chat_id), "meta.json"), meta)
    storage.write_json(os.path.join(_chat_dir(username, chat_id), "messages.json"), [])
    return meta


def load_meta(username, chat_id):
    chat_id = _safe_id(chat_id)
    if not chat_id:
        return None
    return storage.read_json(os.path.join(_chat_dir(username, chat_id), "meta.json"))


def save_meta(username, chat_id, meta):
    """Replace a chat's whole meta document.

    Correct only when the caller genuinely means to replace it. Anything that
    changes one FIELD must use `update_meta` instead -- a whole-document write
    computed from an earlier read is exactly how the running token and cost
    totals were getting lost."""
    meta["updated_at"] = _now()
    storage.write_json(os.path.join(_chat_dir(username, chat_id), "meta.json"), meta)


def update_meta(username, chat_id, change):
    """Change specific fields of a chat's meta under an exclusive lock.

    `change` receives the current meta and mutates it in place. Added because
    `append_messages` was carefully locked and then its caller immediately
    undid that by writing the whole document back to set `model` -- rolling
    the counters straight off again."""
    chat_id = _safe_id(chat_id)
    if not chat_id:
        return {}

    def _apply(meta):
        change(meta)
        meta["updated_at"] = _now()

    return storage.mutate_json(
        os.path.join(_chat_dir(username, chat_id), "meta.json"), _apply, {})


def load_messages(username, chat_id):
    chat_id = _safe_id(chat_id)
    if not chat_id:
        return []
    return storage.read_json(os.path.join(_chat_dir(username, chat_id), "messages.json"), []) or []


def save_messages(username, chat_id, messages):
    storage.write_json(os.path.join(_chat_dir(username, chat_id), "messages.json"), messages)


def list_chats(username):
    out = []
    for path in glob.glob(os.path.join(_user_dir(username), "*")):
        if not os.path.isdir(path):
            continue
        meta = storage.read_json(os.path.join(path, "meta.json"))
        if meta:
            out.append(meta)
    return sorted(out, key=lambda m: m.get("updated_at") or "", reverse=True)


def delete_chat(username, chat_id):
    chat_id = _safe_id(chat_id)
    if not chat_id:
        return False
    d = _chat_dir(username, chat_id)
    if not os.path.isdir(d):
        return False
    meta = load_meta(username, chat_id) or {}
    if meta.get("share_token"):
        revoke_share(meta["share_token"])
    for name in ("meta.json", "messages.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        os.rmdir(d)
    except OSError:
        pass
    return True


def forget_user(username):
    """Account deletion: drop every chat and every share pointing at them."""
    for meta in list_chats(username):
        delete_chat(username, meta["chat_id"])
    # Under the lock, and touching only this user's tokens. Unlocked, a
    # colleague sharing a conversation at the same instant either lost their
    # brand-new link or had a revoked one resurrected.
    def _drop_mine(shares):
        for token in [t for t, entry in shares.items() if entry.get("owner") == username]:
            del shares[token]

    storage.mutate_json(SHARES_PATH, _drop_mine, {})


# ---------- appending a turn ----------

def append_messages(username, chat_id, new_messages, usage=None, title_hint=None):
    """Append to the transcript and roll the counters up into meta.

    Both halves are read-modify-write, which is why they run under
    `storage.mutate_json` rather than load/modify/save. The same user with the
    same conversation open in two browser tabs is not a contrived case -- it
    is how people work -- and unlocked, whichever turn finished second
    rewrote the whole transcript from the version it read, silently deleting
    the other turn and the running totals with it.
    """
    chat_id = _safe_id(chat_id)
    if not chat_id:
        return {}
    messages_path = os.path.join(_chat_dir(username, chat_id), "messages.json")
    meta_path = os.path.join(_chat_dir(username, chat_id), "meta.json")

    # In place, and with no `messages or []` rescue: mutate_json always hands
    # over a real list (the default is materialised for a file that does not
    # exist yet), and `x or []` would silently swap in a DIFFERENT empty list
    # whose mutations never reach the document.
    def _append(messages):
        messages.extend(new_messages)

    messages = storage.mutate_json(messages_path, _append, [])

    def _roll(meta):
        meta["message_count"] = sum(1 for m in messages if m.get("role") in ("user", "assistant"))
        meta["tool_call_count"] = sum(len(m.get("tool_calls") or []) for m in messages)
        if usage:
            meta["total_tokens"] = (meta.get("total_tokens") or 0) + (usage.get("total_tokens") or 0)
            meta["total_cost"] = round((meta.get("total_cost") or 0.0)
                                       + (usage.get("cost") or 0.0), 6)
        if title_hint and (meta.get("title") in (None, "", "New conversation")):
            meta["title"] = derive_title(title_hint)
        meta["updated_at"] = _now()

    return storage.mutate_json(meta_path, _roll, {})


def derive_title(text):
    """First line of the opening question, trimmed. Good enough, and far more
    useful in a sidebar than 'New conversation' forever."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else "New conversation"
    line = " ".join(line.split())
    return (line[:MAX_TITLE - 1] + "…") if len(line) > MAX_TITLE else line


# ---------- sharing ----------

def _load_shares():
    return storage.read_json(SHARES_PATH, {}) or {}


def _save_shares(shares):
    storage.write_json(SHARES_PATH, shares)


def create_share(username, chat_id, include_tools=False):
    """Mint a share token for a chat. Re-sharing an already-shared chat keeps
    the same token, so a link already sent to someone does not silently die
    just because the owner clicked Share twice."""
    meta = load_meta(username, chat_id)
    if not meta:
        return None
    token = meta.get("share_token") or secrets.token_urlsafe(24)
    # _shares.json is one document shared by every user in the app, so two
    # people sharing different conversations at the same moment were racing:
    # one link worked and the other 404'd for no visible reason.
    storage.mutate_json(SHARES_PATH, lambda shares: shares.update({token: {
        "owner": username,
        "chat_id": chat_id,
        "created_at": _now(),
        "include_tools": bool(include_tools),
    }}), {})
    def _stamp(m):
        m["share_token"] = token
        m["share_includes_tools"] = bool(include_tools)
        m["shared_at"] = _now()

    update_meta(username, chat_id, _stamp)
    return {"token": token, "include_tools": bool(include_tools)}


def revoke_share(token):
    captured = {}

    def _remove(shares):
        entry = shares.pop(token, None)
        if entry:
            captured.update(entry)

    storage.mutate_json(SHARES_PATH, _remove, {})
    entry = captured or None
    if entry is None:
        return False
    def _clear(m):
        m["share_token"] = None
        m["share_includes_tools"] = False
        m["shared_at"] = None

    if load_meta(entry["owner"], entry["chat_id"]):
        update_meta(entry["owner"], entry["chat_id"], _clear)
    return True


def resolve_share(token):
    """Public read of a shared transcript. Returns None for an unknown or
    revoked token -- the caller turns that into a 404, never a 403, so a
    revoked link cannot be distinguished from one that never existed."""
    if not token:
        return None
    entry = _load_shares().get(token)
    if not entry:
        return None
    meta = load_meta(entry["owner"], entry["chat_id"])
    if not meta or meta.get("share_token") != token:
        return None
    messages = load_messages(entry["owner"], entry["chat_id"])
    return {
        "meta": {
            "title": meta.get("title"),
            "org_label": meta.get("org_id"),
            "model": meta.get("model"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "shared_by": entry["owner"],
            "include_tools": entry.get("include_tools", False),
        },
        "messages": redact_for_share(messages, entry.get("include_tools", False)),
    }


def redact_for_share(messages, include_tools):
    """Shape the transcript for an anonymous viewer.

    Always dropped: the system prompt (it contains the org inventory), and any
    internal bookkeeping. Tool arguments and results are dropped unless the
    sharer explicitly opted in -- an anonymous viewer has no org visibility to
    evaluate, so the safe default is to show only that a tool ran, not what it
    returned.
    """
    out = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            if not include_tools:
                continue
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "name": m.get("name"), "content": m.get("content")})
            continue
        entry = {"role": role, "content": m.get("content"), "at": m.get("at")}
        if m.get("reasoning"):
            entry["reasoning"] = m["reasoning"]
        if m.get("usage"):
            entry["usage"] = {k: m["usage"].get(k) for k in
                              ("prompt_tokens", "completion_tokens", "total_tokens", "cost")}
        calls = m.get("tool_calls") or []
        if calls:
            entry["tool_calls"] = [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "ok": c.get("ok"),
                    "ms": c.get("ms"),
                    **({"args": c.get("args")} if include_tools else {}),
                }
                for c in calls
            ]
        out.append(entry)
    return out
