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

# Neutralise any local webapp/.env before the app package is imported --
# otherwise a developer's real LLM connection leaks into the test run and
# results depend on a file that is not in the repository.
os.environ["TS_SKIP_ENV_FILE"] = "1"

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

from app import secrets_store, chat_store, chat, llm_config, usage  # noqa: E402

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

# State handed from one check to the next (the chat id these tests build on,
# then the share token minted from it).
#
# A dict rather than function arguments, because pytest reads a test
# function's parameters as FIXTURE NAMES -- `def test_share_redaction(cid)`
# made pytest look for a fixture called `cid`, fail to find one, and error
# out three of these tests. They passed when the file was run directly, which
# is how that went unnoticed. pytest preserves definition order within a
# module, so the sequence still holds either way.
STATE = {}


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
    """Credential resolution, with no shared connection in the environment.

    The personal key is now an admin-only override, so the identity has to
    carry `role: admin` for it to be reachable -- a demoted admin falls back
    to the shared connection like everyone else, and with no shared
    connection configured there is nothing to fall back to.
    """
    print("\nkey storage: require_key resolves and fails in the right order")
    _clear_shared_env()
    admin = {"username": USER, "token_id": "tok1", "role": "admin"}
    check("returns the admin's own key when unlocked", secrets_store.require_key(admin) == KEY)

    plain = {"username": USER, "token_id": "tok1", "role": "user"}
    try:
        secrets_store.require_key(plain)
        check("a non-admin cannot use a personal key", False)
    except secrets_store.KeyMissing:
        check("a non-admin cannot use a personal key", True)

    secrets_store._KEYRING.clear()
    try:
        secrets_store.require_key(admin)
        check("a locked personal key with no shared connection raises", False)
    except (secrets_store.KeyLocked, secrets_store.KeyMissing):
        check("a locked personal key with no shared connection raises", True)

    try:
        secrets_store.require_key({"username": "nobody", "token_id": "tokX", "role": "admin"})
        check("raises KeyMissing when absent", False)
    except secrets_store.KeyMissing:
        check("raises KeyMissing when absent", True)
    secrets_store.unlock(USER, PW, "tok1")


def _clear_shared_env():
    for var in (llm_config.ENV_PROVIDER, llm_config.ENV_API_KEY, llm_config.ENV_ENDPOINT,
                llm_config.ENV_DEFAULT_MODEL, llm_config.ENV_LOCK_MODEL):
        os.environ.pop(var, None)


def _set_shared_env(provider="openrouter", api_key="sk-or-v1-shared000000000000000000000000",
                    endpoint=None, default_model=None, lock=None):
    _clear_shared_env()
    os.environ[llm_config.ENV_PROVIDER] = provider
    os.environ[llm_config.ENV_API_KEY] = api_key
    if endpoint:
        os.environ[llm_config.ENV_ENDPOINT] = endpoint
    if default_model:
        os.environ[llm_config.ENV_DEFAULT_MODEL] = default_model
    if lock:
        os.environ[llm_config.ENV_LOCK_MODEL] = lock


SHARED_KEY = "sk-or-v1-shared000000000000000000000000"


def test_shared_connection_serves_everyone():
    """The headline behaviour: one server-side connection, and every user can
    chat on it with nothing of their own to configure."""
    print("\nshared connection: one env-configured key serves every user")
    _set_shared_env(default_model="anthropic/claude-sonnet-4.5")
    secrets_store._KEYRING.clear()

    for role in ("reader", "user", "admin"):
        ident = {"username": f"person_{role}", "token_id": f"t_{role}", "role": role}
        creds = secrets_store.require_creds(ident)
        check(f"{role} resolves to the shared key", creds["api_key"] == SHARED_KEY)
        check(f"{role} is tagged as using the shared source", creds["source"] == "shared")
        state = secrets_store.effective_state(ident)
        check(f"{role} chat is ready with no key of their own", state["ready"] is True)
        check(f"{role} is told it is the shared connection", state["using"] == "shared")

    check("default model comes from the environment",
          secrets_store.effective_state({"username": "x", "token_id": "t", "role": "user"})
          ["default_model"] == "anthropic/claude-sonnet-4.5")


def test_admin_personal_key_overrides_shared():
    print("\nshared connection: an admin's own unlocked key takes precedence")
    _set_shared_env()
    secrets_store.store_key(USER, PW, KEY, token_id="tokA")
    admin = {"username": USER, "token_id": "tokA", "role": "admin"}
    creds = secrets_store.require_creds(admin)
    check("admin's own key wins", creds["api_key"] == KEY)
    check("tagged as personal", creds["source"] == "personal")
    check("effective state agrees", secrets_store.effective_state(admin)["using"] == "personal")

    # Locked, not absent: chat must keep working on the shared connection
    # rather than dying, which is the whole reason the fallback exists.
    secrets_store.evict("tokA")
    creds = secrets_store.require_creds(admin)
    check("a locked personal key falls back to shared", creds["source"] == "shared")
    state = secrets_store.effective_state(admin)
    check("chat still reported ready", state["ready"] is True)
    check("but the admin is told their own key is locked",
          state["personal_key_locked"] is True)

    # A demoted admin loses the override immediately, without re-authenticating.
    secrets_store.unlock(USER, PW, "tokA")
    demoted = {"username": USER, "token_id": "tokA", "role": "user"}
    check("a demoted admin falls back to shared",
          secrets_store.require_creds(demoted)["source"] == "shared")
    check("and is shown no personal-key panel",
          secrets_store.effective_state(demoted)["personal"] is None)


def test_env_file_loading():
    """The env-file loader (app/env_file.py).

    This exists because the documented way to configure the shared LLM
    connection was systemd's `EnvironmentFile=` -- a systemd directive that
    nothing in this app read. On Windows, creating the file had no effect at
    all, silently: the app reported "no shared LLM connection configured"
    while a correct-looking file sat on disk.
    """
    print("\nenv file: parsing")
    from app import env_file

    def one(line):
        k, v, _n = env_file.parse(line)[0]
        return v

    # The Azure endpoint carries its own '=' in ?api-version=. A naive split
    # truncates it, and the resulting URL fails validation for a reason that
    # points nowhere near the real cause.
    azure = ("https://r.openai.azure.com/openai/deployments/gpt-4o"
             "/chat/completions?api-version=2024-08-01-preview")
    check("an Azure endpoint's own '=' survives",
          one(f"TS_LLM_ENDPOINT={azure}") == azure, one(f"TS_LLM_ENDPOINT={azure}"))
    check("a spaced trailing comment is stripped", one("TS_LLM_LOCK_MODEL=1   # optional") == "1")
    check("a '#' inside a value is kept", one("TS_ADMIN_PASSWORD=pa#ss") == "pa#ss")
    check("double quotes are stripped", one('TS_LLM_PROVIDER="azure"') == "azure")
    check("single quotes are stripped", one("TS_LLM_PROVIDER='azure'") == "azure")
    check("a shell 'export ' prefix is tolerated", one("export TS_LLM_PROVIDER=azure") == "azure")
    check("space around the '=' is trimmed", one("  TS_LLM_PROVIDER = azure  ") == "azure")
    check("an empty value is allowed", one("TS_LLM_DEFAULT_MODEL=") == "")
    check("comments, blanks and junk lines are skipped",
          env_file.parse("# c\n\n   \nnojunkhere\n") == [])

    print("\nenv file: loading, precedence and diagnostics")
    path = os.path.join(_TMP, "probe.env")
    with open(path, "w", encoding="utf-8") as f:
        f.write("TS_LLM_PROVIDER=openrouter\n"
                "TS_LLM_API_KEY=sk-or-v1-fromthefile00000000000000\n"
                "TS_LLM_LOCK_MODEL=1\n"
                "TS_LLM_APIKEY=typo\n"              # a misspelling must be reported
                "TS_LLM_PROVIDER=duplicate\n")

    saved = {k: os.environ.get(k) for k in
             ("TS_ENV_FILE", "TS_SKIP_ENV_FILE", "TS_LLM_PROVIDER", "TS_LLM_API_KEY",
              "TS_LLM_LOCK_MODEL", "TS_LLM_APIKEY")}
    try:
        os.environ.pop("TS_SKIP_ENV_FILE", None)
        os.environ["TS_ENV_FILE"] = path
        for k in ("TS_LLM_PROVIDER", "TS_LLM_API_KEY", "TS_LLM_APIKEY"):
            os.environ.pop(k, None)
        # Pre-set, exactly as systemd or an explicit shell override would be.
        os.environ["TS_LLM_LOCK_MODEL"] = "0"

        applied = env_file.load(verbose=False)
        check("values absent from the environment are filled in",
              applied.get("TS_LLM_PROVIDER") == "openrouter")
        check("the file is reported so the operator can see WHICH one was used",
              env_file.LOADED_FROM == path)
        check("a pre-set environment variable is NOT overwritten",
              os.environ["TS_LLM_LOCK_MODEL"] == "0", os.environ["TS_LLM_LOCK_MODEL"])
        check("and that override is reported, not silent",
              any("already set" in n and "TS_LLM_LOCK_MODEL" in n for n in env_file._REPORT))

        # A typo is the failure mode this has to catch. Without it the app just
        # says "not configured" and the operator stares at a correct-looking file.
        check("an unrecognised key is flagged as a probable typo",
              any("TS_LLM_APIKEY" in n and "spelling" in n for n in env_file._REPORT),
              str(env_file._REPORT))
        check("a duplicated key is flagged", any("more than once" in n for n in env_file._REPORT))
        check("the first of a duplicated pair wins",
              os.environ["TS_LLM_PROVIDER"] == "openrouter")

        # The whole point: the connection becomes usable purely from the file.
        check("the shared connection is configured from the file alone",
              llm_config.configured() is True)

        # Opt-out, so a developer's real key cannot leak into a test run.
        os.environ["TS_SKIP_ENV_FILE"] = "1"
        env_file.load(verbose=False)
        check("TS_SKIP_ENV_FILE disables the file entirely",
              env_file.LOADED_FROM is None)
        check("and says so rather than looking like a missing file",
              any("TS_SKIP_ENV_FILE" in n for n in env_file._REPORT))

        # A named-but-missing file must NOT fall back to a different one. The
        # app coming up on settings the operator did not choose, and cannot
        # see, is worse than it coming up unconfigured.
        os.environ.pop("TS_SKIP_ENV_FILE", None)
        missing = os.path.join(_TMP, "does-not-exist.env")
        os.environ["TS_ENV_FILE"] = missing
        env_file.load(verbose=False)
        check("an explicitly named missing file loads nothing",
              env_file.LOADED_FROM is None, str(env_file.LOADED_FROM))
        check("and does not silently fall back to another file",
              not any(p.endswith(".env") and p != missing
                      for p in [env_file.LOADED_FROM or ""]))
        check("naming the path that was missing",
              any(missing in n for n in env_file._REPORT), str(env_file._REPORT))

        # No file anywhere is normal, not an error -- it is the systemd case,
        # where the variables are already exported.
        os.environ.pop("TS_ENV_FILE", None)
        report = " ".join(env_file.startup_report())
        check("with no file at all, the report lists every path searched",
              "Looked for" in report or "Loaded configuration" in report, report[:200])

        check("no secret value is ever printed in the report",
              "sk-or-v1-fromthefile00000000000000" not in " ".join(env_file.startup_report()))
        desc = env_file.describe_loaded()
        check("diagnostics report a secret as present, never its value",
              desc["settings"].get("TS_LLM_API_KEY") in (None, "(set)"),
              str(desc["settings"].get("TS_LLM_API_KEY")))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ["TS_SKIP_ENV_FILE"] = "1"
        env_file.load(verbose=False)


def test_shared_connection_validation():
    print("\nshared connection: bad configuration is reported, not swallowed")
    _clear_shared_env()
    check("unset means not configured", llm_config.configured() is False)
    check("and chat is not ready",
          secrets_store.effective_state({"username": "x", "token_id": "t",
                                         "role": "user"})["ready"] is False)

    os.environ[llm_config.ENV_API_KEY] = "some-key"
    os.environ[llm_config.ENV_PROVIDER] = "not-a-provider"
    check("an unknown provider is rejected", llm_config.configured() is False)
    state = llm_config.public_state()
    check("and says so specifically", "not recognised" in (state["config_error"] or ""))
    check("distinguishing 'present but wrong' from 'absent'",
          state["present_but_invalid"] is True)

    os.environ[llm_config.ENV_PROVIDER] = "azure"
    os.environ[llm_config.ENV_ENDPOINT] = "https://foo.openai.azure.com"
    check("an Azure resource root without the deployment path is rejected",
          llm_config.configured() is False)
    os.environ[llm_config.ENV_ENDPOINT] = (
        "https://foo.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
        "?api-version=2024-08-01-preview")
    check("a full Azure chat-completions URL is accepted", llm_config.configured() is True)
    check("and the deployment becomes the model",
          llm_config.default_model() == "gpt-4o", llm_config.default_model())

    os.environ[llm_config.ENV_LOCK_MODEL] = "1"
    check("a locked model binds a user",
          secrets_store.effective_state({"username": "x", "token_id": "t",
                                         "role": "user"})["model_locked"] is True)
    check("but never an admin",
          secrets_store.effective_state({"username": "y", "token_id": "t2",
                                         "role": "admin"})["model_locked"] is False)

    # No admin key hint or endpoint should ever reach a non-admin payload.
    user_view = llm_config.public_state()
    check("a non-admin view carries no key hint", "hint" not in user_view)
    check("a non-admin view carries no endpoint", "endpoint" not in user_view)
    check("an admin view does", llm_config.admin_state()["hint"] is not None)
    _clear_shared_env()


# ---------------------------------------------------------------- usage ledger

def test_usage_ledger():
    print("\nusage: per-user, per-day and per-org attribution")
    for path in __import__("glob").glob(os.path.join(storage.usage_root(), "*.jsonl")):
        os.remove(path)

    usage.record_turn("alice", chat_id="c1", org_id="acme_prod", model="m1",
                      provider="openrouter", source="shared",
                      usage={"prompt_tokens": 100, "completion_tokens": 50,
                             "total_tokens": 150, "cost": 0.002},
                      tool_calls=3, tool_rounds=2, duration_ms=4000)
    usage.record_turn("alice", chat_id="c2", org_id="acme_prod", model="m1",
                      usage={"total_tokens": 50, "cost": 0.001}, duration_ms=2000)
    usage.record_turn("bob", chat_id="c3", org_id="other_org", model="m2",
                      usage={"total_tokens": 900, "cost": 0.05}, duration_ms=9000)
    # No org: normalizing a standalone log needs no connection at all.
    usage.record_turn("bob", chat_id="c4", model="m2", usage={"total_tokens": 10})
    # A failed turn still counts as an attempt.
    usage.record_turn("carol", ok=False, error_code="key_missing")

    r = usage.report(days=2)
    check("every turn counted", r["totals"]["turns"] == 5, r["totals"]["turns"])
    check("failures counted separately", r["totals"]["failed_turns"] == 1)
    check("tokens summed", r["totals"]["total_tokens"] == 1110, r["totals"]["total_tokens"])
    check("three users active", r["active_users"] == 3)

    by_user = {row["username"]: row for row in r["by_user"]}
    check("alice's tokens attributed to alice", by_user["alice"]["total_tokens"] == 200)
    check("bob's tokens attributed to bob", by_user["bob"]["total_tokens"] == 910)
    check("ranked by consumption, heaviest first", r["by_user"][0]["username"] == "bob")
    check("alice's tool calls counted", by_user["alice"]["tool_calls"] == 3)
    check("average turn duration derived", by_user["alice"]["avg_seconds_per_turn"] == 3.0,
          by_user["alice"]["avg_seconds_per_turn"])

    by_org = {row["org_id"]: row for row in r["by_org"]}
    check("per-org breakdown present", by_org["acme_prod"]["total_tokens"] == 200)
    check("orgless turns get their own bucket", "(no org)" in by_org)

    check("the day series covers the window", len(r["by_day"]) == 2)
    check("the series is chronological", r["by_day"][0]["date"] < r["by_day"][1]["date"])
    today = r["by_day"][-1]
    check("today's bucket holds today's turns", today["turns"] == 5, today["turns"])

    check("cost is reported when the provider gives one",
          r["totals"]["cost_available"] is True)

    # Azure reports no cost. $0.00 and "cannot say" must not look alike.
    for path in __import__("glob").glob(os.path.join(storage.usage_root(), "*.jsonl")):
        os.remove(path)
    usage.record_turn("dave", model="gpt-4o", provider="azure",
                      usage={"total_tokens": 500})
    az = usage.report(days=1)
    check("an Azure-only window reports tokens", az["totals"]["total_tokens"] == 500)
    check("and admits cost is unavailable rather than showing $0",
          az["totals"]["cost_available"] is False)

    mine = usage.my_summary("dave", days=1)
    check("a user can read their own summary", mine["totals"]["total_tokens"] == 500)
    check("a user's own summary excludes other people",
          usage.my_summary("alice", days=1)["totals"]["turns"] == 0)

    usage.forget_user("dave")
    check("deleting an account removes its usage records",
          usage.report(days=1)["totals"]["turns"] == 0)


def test_usage_concurrent_appends():
    """The reason the ledger is append-only rather than a totals document.

    Twenty threads writing at once, which is what an unlocked
    read-modify-write over shared counters cannot survive.
    """
    print("\nusage: concurrent writers do not lose records")
    import threading
    for path in __import__("glob").glob(os.path.join(storage.usage_root(), "*.jsonl")):
        os.remove(path)

    def worker(n):
        for i in range(10):
            usage.record_turn(f"u{n}", usage={"total_tokens": 1})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r = usage.report(days=1)
    check("all 200 records survived", r["totals"]["turns"] == 200, r["totals"]["turns"])
    check("and every token is accounted for", r["totals"]["total_tokens"] == 200)
    check("across all 20 users", r["active_users"] == 20)


def test_mutator_cannot_replace_the_whole_store():
    """`mutate_json` treats a non-None return as a replacement document.

    That makes `lambda d: d.pop(k, None) and None` a trap: it reads as
    "remove and return nothing", but `{} and None` is `{}`, so popping a
    falsy record replaced the ENTIRE store with an empty dict -- every other
    user's wrapped key destroyed. `forget_user` was written that way. This
    pins the corrected behaviour.
    """
    print("\nstorage: deleting one record cannot wipe the store")
    records = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    records["ghost"] = {}                     # the falsy record that triggered it
    storage.write_json(secrets_store.LLM_KEYS_PATH, records)

    secrets_store.forget_user("ghost")
    after = storage.read_json(secrets_store.LLM_KEYS_PATH, {})
    check("the targeted record is gone", "ghost" not in after)
    check("every other record survives", USER in after, str(list(after)))
    check("and the surviving key material is intact",
          bool(after.get(USER, {}).get("ciphertext")))

    # And the contract directly: the return value must be IGNORED, so that
    # the dangerous one-liner behaves the same as the careful named function.
    probe = os.path.join(_TMP, "mutator_contract.json")
    storage.write_json(probe, {"a": 1, "b": {}})
    storage.mutate_json(probe, lambda d: d.pop("b", None), {})
    check("a mutator's return value is ignored, so pop() cannot replace the document",
          storage.read_json(probe, {}) == {"a": 1}, str(storage.read_json(probe, {})))
    storage.mutate_json(probe, lambda d: d.pop("a", None), {})
    check("even when what it returns is truthy",
          storage.read_json(probe, {}) == {}, str(storage.read_json(probe, {})))

    # Replacing the whole document is still possible -- it just has to say so.
    def _replace(d):
        d.clear()
        d.update({"replaced": True})

    storage.mutate_json(probe, _replace, {})
    check("an explicit clear+update replaces it",
          storage.read_json(probe, {}) == {"replaced": True})

    # A list document, and a caller's default that must not be mutated.
    shared_default = []
    fresh = os.path.join(_TMP, "mutator_list.json")
    storage.mutate_json(fresh, lambda items: items.append(1), shared_default)
    check("a list document is appended to", storage.read_json(fresh, None) == [1])
    check("the caller's default object is left alone", shared_default == [])


def test_locked_mutation_is_serialised():
    """`storage.mutate_json` under contention.

    The pattern being tested is the one that was losing data all over the
    app: read a shared document, change one entry, write the whole thing
    back. Unlocked, the assertion below fails by a wide margin.
    """
    print("\nstorage: mutate_json does not lose concurrent updates")
    import threading
    path = os.path.join(_TMP, "concurrency_probe.json")
    if os.path.exists(path):
        os.remove(path)

    def bump(key):
        for i in range(25):
            storage.mutate_json(path, lambda d: d.update({f"{key}_{i}": i}), {})

    threads = [threading.Thread(target=bump, args=(f"k{n}",)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = storage.read_json(path, {})
    check("all 200 keys present", len(final) == 200, f"got {len(final)}")

    # And a counter, which is the harsher test: every increment must see the
    # previous one, not a stale snapshot.
    counter_path = os.path.join(_TMP, "counter_probe.json")
    if os.path.exists(counter_path):
        os.remove(counter_path)

    def increment():
        for _ in range(50):
            storage.mutate_json(counter_path,
                                lambda d: d.update({"n": (d.get("n") or 0) + 1}), {})

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("counter reached 400 with no lost increments",
          storage.read_json(counter_path, {}).get("n") == 400,
          str(storage.read_json(counter_path, {}).get("n")))

    check("locks are re-entrant within a thread",
          storage.mutate_json(path, lambda d: storage.write_json(path, d)) is not None)


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
    STATE["cid"] = cid


def test_chat_id_traversal():
    print("\nchat store: a chat id cannot walk out of the user's directory")
    for bad in ("../../etc/passwd", "..\\..\\x", "./x", ".hidden"):
        check(f"rejects {bad!r}", chat_store.load_meta(USER, bad) is None)


def test_share_redaction():
    cid = STATE["cid"]
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
    STATE["token"] = share["token"]


def test_share_revoke():
    cid, token = STATE["cid"], STATE["token"]
    print("\nsharing: revoking is immediate and total")
    check("resolves before revoke", chat_store.resolve_share(token) is not None)
    check("revoke reports success", chat_store.revoke_share(token) is True)
    check("resolves to nothing after", chat_store.resolve_share(token) is None)
    check("meta no longer claims a share", chat_store.load_meta(USER, cid)["share_token"] is None)
    check("unknown token is indistinguishable", chat_store.resolve_share("made-up-token") is None)


def test_delete_chat():
    cid = STATE["cid"]
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
        test_shared_connection_serves_everyone()
        test_admin_personal_key_overrides_shared()
        test_env_file_loading()
        test_shared_connection_validation()
        test_usage_ledger()
        test_usage_concurrent_appends()
        test_mutator_cannot_replace_the_whole_store()
        test_locked_mutation_is_serialised()

        test_chat_crud()
        test_chat_id_traversal()
        test_share_redaction()
        test_share_revoke()
        test_delete_chat()

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
