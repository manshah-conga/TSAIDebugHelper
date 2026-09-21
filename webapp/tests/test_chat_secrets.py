"""
Tests for the password-wrapped LLM key store, chat persistence, sharing, and
the tool-schema sanitizer.

These are the parts where a bug is silent rather than loud: a key that
"works" but was never really encrypted, a share link that leaks org internals,
a schema that a strict model rejects. Run with:

    cd webapp && python -m pytest tests/test_chat_secrets.py -q

or standalone:  python tests/test_chat_secrets.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WEBAPP = os.path.dirname(HERE)
sys.path.insert(0, WEBAPP)

# Point every storage path at a throwaway directory BEFORE importing anything
# that computes paths at import time.
_TMP = tempfile.mkdtemp(prefix="ts-chat-test-")

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

from app import secrets_store, chat_store, chat  # noqa: E402

secrets_store.LLM_KEYS_PATH = os.path.join(storage.AUTH_ROOT, "llm_keys.json")
chat_store.CHATS_ROOT = os.path.join(_TMP, "chats")
chat_store.SHARES_PATH = os.path.join(chat_store.CHATS_ROOT, "_shares.json")

# PBKDF2 at 400k rounds x ~20 derivations makes this suite slow for no test
# value; the rounds count is not what is under test.
secrets_store.KEK_ROUNDS = 1000

USER = "tester"
PW = "correct horse battery"
KEY = "sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789abcdef019f2a"

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- crypto

def test_roundtrip():
    print("\nkey storage: round trip")
    secrets_store.store_key(USER, PW, KEY, token_id="tok1")
    check("unlocked for the storing session", secrets_store.key_for_session("tok1") == KEY)

    state = secrets_store.public_state(USER, "tok1")
    check("state says configured", state["configured"] is True)
    check("state says unlocked", state["unlocked"] is True)
    check("hint ends with the last 4 chars", state["hint"].endswith("9f2a"), state["hint"])
    check("hint does not contain the key", KEY not in json.dumps(state))


def test_ciphertext_is_opaque():
    print("\nkey storage: the file alone is useless")
    raw = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    blob = json.dumps(raw)
    check("plaintext key absent from disk", KEY not in blob)
    check("password absent from disk", PW not in blob)
    check("no field named like a key", not any(
        k in raw[USER] for k in ("api_key", "key", "plaintext")))
    for field in ("kek_salt", "wrapped_dek", "dek_nonce", "ciphertext", "key_nonce"):
        check(f"{field} present", field in raw[USER])


def test_locked_after_restart():
    print("\nkey storage: a restart locks, it does not lose")
    secrets_store._KEYRING.clear()           # exactly what a process restart does
    check("no session has the key", secrets_store.key_for_session("tok1") is None)
    state = secrets_store.public_state(USER, "tok1")
    check("still configured", state["configured"] is True)
    check("but locked", state["unlocked"] is False)

    secrets_store.unlock(USER, PW, "tok2")
    check("unlocks with the right password", secrets_store.key_for_session("tok2") == KEY)


def test_wrong_password():
    print("\nkey storage: wrong password")
    try:
        secrets_store.unlock(USER, "not the password", "tok3")
        check("rejects a wrong password", False, "no exception raised")
    except secrets_store.BadPassword:
        check("rejects a wrong password", True)
    check("nothing landed in the keyring", secrets_store.key_for_session("tok3") is None)


def test_aad_binds_to_user():
    print("\nkey storage: a record moved to another user does not decrypt")
    records = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    stolen = dict(records[USER])
    stolen["username"] = "attacker"
    records["attacker"] = stolen
    storage.write_json(secrets_store.LLM_KEYS_PATH, records)
    try:
        secrets_store.unlock("attacker", PW, "tok9")
        check("AEAD rejects the moved record", False, "it decrypted")
    except secrets_store.BadPassword:
        check("AEAD rejects the moved record", True)
    records = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    del records["attacker"]
    storage.write_json(secrets_store.LLM_KEYS_PATH, records)


def test_self_password_change_rewraps():
    print("\nkey storage: self-service password change re-wraps")
    new_pw = "a different passphrase"
    before = storage.read_json(secrets_store.LLM_KEYS_PATH, {})[USER]["ciphertext"]
    ok = secrets_store.rewrap_for_new_password(USER, PW, new_pw)
    check("rewrap reported success", ok is True)
    after = storage.read_json(secrets_store.LLM_KEYS_PATH, {})[USER]["ciphertext"]
    check("the key's own ciphertext is untouched", before == after)

    secrets_store._KEYRING.clear()
    secrets_store.unlock(USER, new_pw, "tok4")
    check("new password unlocks", secrets_store.key_for_session("tok4") == KEY)
    try:
        secrets_store.unlock(USER, PW, "tok5")
        check("old password no longer works", False, "it still unlocked")
    except secrets_store.BadPassword:
        check("old password no longer works", True)
    secrets_store.rewrap_for_new_password(USER, new_pw, PW)   # restore for later tests
    secrets_store._KEYRING.clear()
    secrets_store.unlock(USER, PW, "tok1")


def test_admin_reset_destroys_key():
    print("\nkey storage: an admin reset destroys the key, visibly")
    cleared = secrets_store.note_password_reset_by_admin(USER)
    check("reported as cleared", cleared is True)
    state = secrets_store.public_state(USER, "tok1")
    check("no longer configured", state["configured"] is False)
    check("reason is explainable to the user",
          state["cleared_reason"] == "password_reset_by_admin", state["cleared_reason"])
    check("keyring emptied for that user", secrets_store.key_for_session("tok1") is None)
    raw = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    check("no ciphertext left behind", "ciphertext" not in raw.get(USER, {}))
    secrets_store.store_key(USER, PW, KEY, token_id="tok1")   # restore


def test_require_key_errors():
    print("\nkey storage: require_key raises the right kind of failure")
    ident = {"username": USER, "token_id": "tok1"}
    check("returns the key when unlocked", secrets_store.require_key(ident) == KEY)

    secrets_store._KEYRING.clear()
    try:
        secrets_store.require_key(ident)
        check("raises KeyLocked when locked", False)
    except secrets_store.KeyLocked:
        check("raises KeyLocked when locked", True)
    except secrets_store.KeyMissing:
        check("raises KeyLocked when locked", False, "raised KeyMissing instead")

    try:
        secrets_store.require_key({"username": "nobody", "token_id": "tokX"})
        check("raises KeyMissing when absent", False)
    except secrets_store.KeyMissing:
        check("raises KeyMissing when absent", True)
    secrets_store.unlock(USER, PW, "tok1")


# ---------------------------------------------------------------- chat store

def test_chat_crud():
    print("\nchat store: create, append, list")
    meta = chat_store.create_chat(USER, org_id="acme_prod", model="anthropic/claude-sonnet-4.5")
    cid = meta["chat_id"]
    chat_store.append_messages(USER, cid, [
        {"role": "user", "content": "Why is Discount_Percent__c wrong?", "at": "2026-09-14T09:00:00Z"},
        {"role": "assistant", "content": "Two things write it.",
         "tool_calls": [{"id": "c1", "name": "find_field_writers",
                         "args": {"org_id": "acme_prod", "field_api_name": "Discount_Percent__c"},
                         "ok": True, "ms": 88}]},
        {"role": "tool", "tool_call_id": "c1", "name": "find_field_writers",
         "content": '{"writers": [{"component": "APTS_PricingTrigger"}]}'},
    ], usage={"total_tokens": 4892, "cost": 0.021}, title_hint="Why is Discount_Percent__c wrong?")

    meta = chat_store.load_meta(USER, cid)
    check("title derived from the question", meta["title"].startswith("Why is Discount"), meta["title"])
    check("tokens rolled up", meta["total_tokens"] == 4892)
    check("cost rolled up", abs(meta["total_cost"] - 0.021) < 1e-9)
    check("tool calls counted", meta["tool_call_count"] == 1)
    check("appears in the list", any(c["chat_id"] == cid for c in chat_store.list_chats(USER)))
    return cid


def test_chat_id_traversal():
    print("\nchat store: a chat id cannot walk out of the user's directory")
    for bad in ("../../etc/passwd", "..\\..\\x", "./x", ".hidden"):
        check(f"rejects {bad!r}", chat_store.load_meta(USER, bad) is None)


def test_share_redaction(cid):
    print("\nsharing: tool internals are hidden by default")
    share = chat_store.create_share(USER, cid, include_tools=False)
    shared = chat_store.resolve_share(share["token"])
    blob = json.dumps(shared)

    check("transcript resolves", shared is not None)
    check("system prompt excluded", '"role": "system"' not in blob)
    check("tool RESULT content excluded", "APTS_PricingTrigger" not in blob)
    check("tool ARGUMENTS excluded", "Discount_Percent__c" not in
          json.dumps([c for m in shared["messages"] for c in (m.get("tool_calls") or [])]))
    check("tool NAME still shown", "find_field_writers" in blob)
    check("the answer text survives", "Two things write it" in blob)

    print("\nsharing: opting in includes them")
    share = chat_store.create_share(USER, cid, include_tools=True)
    shared = chat_store.resolve_share(share["token"])
    blob = json.dumps(shared)
    check("tool result content included", "APTS_PricingTrigger" in blob)
    check("same token reused, so sent links keep working",
          share["token"] == chat_store.load_meta(USER, cid)["share_token"])
    return share["token"]


def test_share_revoke(cid, token):
    print("\nsharing: revoking is immediate and total")
    check("resolves before revoke", chat_store.resolve_share(token) is not None)
    check("revoke reports success", chat_store.revoke_share(token) is True)
    check("resolves to nothing after", chat_store.resolve_share(token) is None)
    check("meta no longer claims a share", chat_store.load_meta(USER, cid)["share_token"] is None)
    check("unknown token is indistinguishable", chat_store.resolve_share("made-up-token") is None)


def test_delete_chat(cid):
    print("\nchat store: delete")
    check("delete reports success", chat_store.delete_chat(USER, cid) is True)
    check("gone from the list", not any(c["chat_id"] == cid for c in chat_store.list_chats(USER)))
    check("deleting twice is not an error", chat_store.delete_chat(USER, cid) is False)


# ---------------------------------------------------------------- agent glue

def test_schema_sanitizer():
    print("\nagent: schema sanitizer flattens Optional[...] for strict models")
    schema = {
        "type": "object",
        "properties": {
            "org_id": {"type": "string"},
            "label": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "store": {"type": "boolean", "default": False},
        },
        "required": ["org_id"],
        "$defs": {"Unused": {"type": "string"}},
    }
    out = chat._sanitize_schema(schema)
    check("anyOf removed", "anyOf" not in out["properties"]["label"])
    check("non-null branch kept", out["properties"]["label"]["type"] == "string")
    check("$defs stripped", "$defs" not in out)
    check("plain fields untouched", out["properties"]["org_id"]["type"] == "string")
    check("required preserved", out["required"] == ["org_id"])
    check("input not mutated", "anyOf" in schema["properties"]["label"])


def test_tool_policy():
    print("\nagent: tool policy")
    check("credential tools are excluded outright",
          {"create_org_connection", "refresh_org"} == chat.EXCLUDED_TOOLS)
    check("excluded tools are in no allow-list",
          not (chat.EXCLUDED_TOOLS & (chat.BASE_TOOLS | chat.ORG_TOOLS | chat.WRITE_TOOLS)))
    check("set_org_visibility needs confirmation", "set_org_visibility" in chat.WRITE_TOOLS)
    check("no write tool is auto-run", not (chat.WRITE_TOOLS & (chat.BASE_TOOLS | chat.ORG_TOOLS)))
    check("base set is small enough to be cheap", len(chat.BASE_TOOLS) <= 6)


def test_truncation():
    print("\nagent: oversized tool results are cut, and say so")
    small, was_cut = chat._truncate({"a": 1})
    check("small results pass through", was_cut is False)
    big, was_cut = chat._truncate({"blob": "x" * (chat.MAX_TOOL_RESULT_BYTES + 5000)})
    check("large results are cut", was_cut is True)
    check("the cut is announced to the model", "TRUNCATED" in big)
    check("cut to roughly the cap", len(big) < chat.MAX_TOOL_RESULT_BYTES + 400)


def test_provider_message_shape():
    print("\nagent: display fields never reach the provider")
    stored = [
        {"role": "user", "content": "hi", "at": "2026-09-14T09:00:00Z", "org_id": "acme"},
        {"role": "assistant", "content": "", "at": "...", "reasoning": "hmm",
         "tool_calls": [{"id": "c1", "name": "list_orgs", "args": {}, "ok": True, "ms": 12,
                         "result": {"acme": {}}, "preview": "1 key"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "list_orgs", "content": "{}", "at": "..."},
    ]
    wire = chat.to_provider_messages({"role": "system", "content": "sys"}, stored)
    blob = json.dumps(wire)
    check("system prompt is first", wire[0]["role"] == "system")
    for junk in ('"at"', '"ms"', '"ok"', '"preview"', '"org_id"', '"result"'):
        check(f"{junk} stripped", junk not in blob)
    check("tool call converted to provider shape",
          wire[2]["tool_calls"][0]["function"]["name"] == "list_orgs")
    check("arguments serialized as a string",
          isinstance(wire[2]["tool_calls"][0]["function"]["arguments"], str))


def test_tool_call_accumulation():
    print("\nagent: streamed tool-call fragments merge by index, not id")
    from app import llm
    acc = {}
    llm.accumulate_tool_calls(acc, [{"index": 0, "id": "call_a",
                                     "function": {"name": "find_field_writers", "arguments": '{"org'}}])
    llm.accumulate_tool_calls(acc, [{"index": 0, "function": {"arguments": '_id": "acme", "fi'}}])
    llm.accumulate_tool_calls(acc, [{"index": 0, "function": {"arguments": 'eld_api_name": "X__c"}'}}])
    llm.accumulate_tool_calls(acc, [{"index": 1, "id": "call_b",
                                     "function": {"name": "list_orgs", "arguments": "{}"}}])
    calls = llm.finalize_tool_calls(acc)
    check("both calls recovered", len(calls) == 2)
    check("fragments reassembled in order",
          calls[0]["args"] == {"org_id": "acme", "field_api_name": "X__c"}, calls[0]["args"])
    check("id from the opening fragment kept", calls[0]["id"] == "call_a")
    check("second call intact", calls[1]["name"] == "list_orgs")

    bad = {}
    llm.accumulate_tool_calls(bad, [{"index": 0, "id": "c", "function": {"name": "x", "arguments": "{not json"}}])
    out = llm.finalize_tool_calls(bad)
    check("malformed arguments surface as an error, not a crash", out[0]["parse_error"] is not None)


def main():
    try:
        test_roundtrip()
        test_ciphertext_is_opaque()
        test_locked_after_restart()
        test_wrong_password()
        test_aad_binds_to_user()
        test_self_password_change_rewraps()
        test_admin_reset_destroys_key()
        test_require_key_errors()

        cid = test_chat_crud()
        test_chat_id_traversal()
        token = test_share_redaction(cid)
        test_share_revoke(cid, token)
        test_delete_chat(cid)

        test_schema_sanitizer()
        test_tool_policy()
        test_truncation()
        test_provider_message_shape()
        test_tool_call_accumulation()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
