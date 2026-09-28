"""
End-to-end checks over the HTTP surface for the three changes that alter who
can do what:

  1. The LLM connection is server-configured and shared, so an ordinary user
     can chat with nothing of their own set up.
  2. Only admins can change an LLM connection. A `user` or `reader` has no
     endpoint that will let them, not merely no button.
  3. Usage is attributed per account and readable by admins; a non-admin can
     read their own figures and nobody else's.

Plus the two multi-user guards: a second fetch of the same org is refused
while one is in flight, and the status route reports real progress.

Run:  python -m pytest tests/test_llm_and_usage_api.py -q
 or:  python tests/test_llm_and_usage_api.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point storage at a scratch dir BEFORE the app imports it.
_TMP = tempfile.mkdtemp(prefix="ts-llm-api-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"

# A shared connection has to exist before the app's lifespan runs, since that
# is where the startup report is printed.
os.environ["TS_LLM_PROVIDER"] = "openrouter"
os.environ["TS_LLM_API_KEY"] = "sk-or-v1-testtesttesttesttesttesttest"
os.environ["TS_LLM_DEFAULT_MODEL"] = "anthropic/claude-sonnet-4.5"
os.environ.pop("TS_LLM_ENDPOINT", None)
os.environ.pop("TS_LLM_LOCK_MODEL", None)

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

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import secrets_store, usage, onboarding, llm_config, chat_store  # noqa: E402
from app.common_now import iso_now  # noqa: E402

# Every module that computes a path at IMPORT time has to be redirected by
# hand. Missing one does not fail loudly -- it quietly writes into the real
# data/ directory -- so chat_store is pinned here even though this file does
# not currently create a chat.
secrets_store.LLM_KEYS_PATH = os.path.join(storage.AUTH_ROOT, "llm_keys.json")
chat_store.CHATS_ROOT = os.path.join(_TMP, "chats")
chat_store.SHARES_PATH = os.path.join(chat_store.CHATS_ROOT, "_shares.json")
secrets_store.KEK_ROUNDS = 1000

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def client_for(username, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def main():
    with TestClient(app) as boot:
        admin = client_for("admin", "adminpassword123")
        for name, role in (("dana", "user"), ("rex", "reader")):
            r = admin.post("/api/admin/users",
                           json={"username": name, "password": "password1234", "role": role})
            assert r.status_code == 200, r.text
        dana = client_for("dana", "password1234")     # role: user
        rex = client_for("rex", "password1234")       # role: reader

        # ---------- 1. the shared connection serves everyone ----------
        print("\nshared LLM connection: available to every signed-in account")
        for label, c in (("admin", admin), ("user", dana), ("reader", rex)):
            s = c.get("/api/chat/key").json()
            check(f"{label} sees chat as ready", s["ready"] is True, str(s))
            check(f"{label} is on the shared connection", s["using"] == "shared")
            check(f"{label} gets the server's default model",
                  s["default_model"] == "anthropic/claude-sonnet-4.5", str(s["default_model"]))
        check("a non-admin is not offered management", dana.get("/api/chat/key").json()
              ["can_manage"] is False)
        check("an admin is", admin.get("/api/chat/key").json()["can_manage"] is True)
        check("a non-admin is sent no personal-key detail at all",
              dana.get("/api/chat/key").json()["personal"] is None)

        # ---------- 2. only admins can change it ----------
        print("\nLLM connection: only an admin can change it")
        body = {"api_key": "sk-or-v1-whatever0000000000000000000",
                "password": "password1234", "provider": "openrouter"}
        check("a user cannot store an LLM key",
              dana.post("/api/chat/key", json=body).status_code == 403)
        check("a reader cannot either",
              rex.post("/api/chat/key", json=body).status_code == 403)
        check("a user cannot delete the stored key",
              dana.delete("/api/chat/key").status_code == 403)
        check("a user cannot unlock one",
              dana.post("/api/chat/unlock", json={"password": "password1234"}).status_code == 403)

        # Reading the shared connection's state is open, because a user whose
        # chat is broken has to be able to see that it is a server problem.
        check("a user may READ the connection state", rex.get("/api/llm").status_code == 200)
        user_view = dana.get("/api/llm").json()
        check("but the key hint is withheld from them", "hint" not in user_view, str(user_view))
        check("and so is the endpoint", "endpoint" not in user_view)
        admin_view = admin.get("/api/llm").json()
        check("an admin sees the key hint", admin_view.get("hint") is not None)
        check("an admin is told which env vars to edit",
              admin_view["env_vars"]["api_key"] == "TS_LLM_API_KEY")
        check("no response anywhere carries the key itself",
              os.environ["TS_LLM_API_KEY"] not in (admin.get("/api/llm").text
                                                   + admin.get("/api/chat/key").text))

        # ---------- model locking ----------
        print("\nmodel choice: locked only when the operator asks for it")
        check("by default a user may set their model",
              dana.post("/api/chat/default-model",
                        json={"model": "some/model"}).status_code == 200)
        os.environ["TS_LLM_LOCK_MODEL"] = "1"
        check("with the lock set, a user may not",
              dana.post("/api/chat/default-model",
                        json={"model": "other/model"}).status_code == 403)
        check("an admin still may",
              admin.post("/api/chat/default-model",
                         json={"model": "other/model"}).status_code == 200)

        # The lock has to bite on the paths that actually SPEND the key, not
        # just on the "remember my choice" route. Enforcing it only there made
        # it decorative: a client putting `model` in the turn body, or in the
        # body of POST /api/chats, chose whatever it liked and the shared key
        # paid for it.
        r = dana.post("/api/chats", json={"title": "t", "model": "evil/model"})
        check("creating a chat cannot smuggle a different model past the lock",
              r.status_code == 200 and r.json()["model"] == "anthropic/claude-sonnet-4.5",
              r.text[:160])
        locked_chat = r.json()["chat_id"]
        # A turn is the real test. It is expected to fail at the provider (the
        # key is fake), so what matters is which model it was dispatched on.
        dana.post(f"/api/chats/{locked_chat}/messages",
                  json={"content": "hello", "model": "evil/model-2"})
        from app import chat_store as _cs
        meta = _cs.load_meta("dana", locked_chat)
        check("a turn cannot smuggle one either",
              meta.get("model") == "anthropic/claude-sonnet-4.5", str(meta.get("model")))
        os.environ.pop("TS_LLM_LOCK_MODEL")

        # ---------- the server's default model must reach a turn ----------
        # A user with no personal key has no default_model of their own. The
        # fallback previously read that personal record, so TS_LLM_DEFAULT_MODEL
        # never arrived and a turn failed with a flat "Pick a model first" for
        # any caller that did not name a model explicitly -- API and MCP
        # clients, in other words, since the browser always sends one.
        print("\nmodel choice: the server's default reaches a turn unaided")
        r = dana.post("/api/chats", json={"title": "no model named"})
        check("a chat created with no model inherits the server's",
              r.json()["model"] == "anthropic/claude-sonnet-4.5", r.text[:160])
        bare = dana.post("/api/chats", json={"title": "bare"}).json()["chat_id"]
        r = dana.post(f"/api/chats/{bare}/messages", json={"content": "hello"})
        check("a turn with no model named is not rejected as 'pick a model'",
              r.status_code != 400, f"{r.status_code} {r.text[:160]}")

        # ---------- 3. usage ----------
        print("\nusage: attributed per account, readable by the right people")
        # Start from an empty ledger. The model-lock checks above ran real
        # turns, and a turn that fails at the provider is still recorded --
        # deliberately -- so exact counts here have to be measured from a
        # known-clean state rather than assumed.
        import glob as _glob
        for _p in _glob.glob(os.path.join(storage.usage_root(), "*.jsonl")):
            os.remove(_p)

        usage.record_turn("dana", chat_id="c1", org_id="acme_prod", model="m1",
                          source="shared", usage={"total_tokens": 1200, "cost": 0.01},
                          tool_calls=4, duration_ms=5000)
        usage.record_turn("admin", chat_id="c2", org_id="acme_prod", model="m1",
                          source="shared", usage={"total_tokens": 300, "cost": 0.002})
        usage.record_turn("rex", ok=False, error_code="key_missing")

        check("a user cannot read the usage report",
              dana.get("/api/admin/usage").status_code == 403)
        check("a reader cannot either", rex.get("/api/admin/usage").status_code == 403)

        r = admin.get("/api/admin/usage?days=2")
        check("an admin can", r.status_code == 200, r.text[:200])
        report = r.json()
        check("every turn is counted", report["totals"]["turns"] == 3, str(report["totals"]))
        rows = {row["username"]: row for row in report["by_user"]}
        check("dana's tokens are attributed to dana", rows["dana"]["total_tokens"] == 1200)
        check("admin's to admin", rows["admin"]["total_tokens"] == 300)
        check("a failed turn is recorded against its user",
              rows["rex"]["failed_turns"] == 1)
        check("the per-org breakdown is present",
              any(o["org_id"] == "acme_prod" for o in report["by_org"]))
        check("the per-day series covers the window", len(report["by_day"]) == 2)
        check("a per-user drilldown narrows the whole report",
              admin.get("/api/admin/usage?days=2&username=dana").json()["totals"]["turns"] == 1)

        mine = dana.get("/api/usage/me?days=2")
        check("a user can read their OWN usage", mine.status_code == 200)
        check("and it contains only their own", mine.json()["totals"]["total_tokens"] == 1200)
        check("a reader's own summary is theirs alone",
              rex.get("/api/usage/me?days=2").json()["totals"]["turns"] == 1)

        # ---------- 4. progress reporting ----------
        print("\norg fetch: the status route reports real progress")
        onboarding.JOBS["probe_org"] = {"status": "queued", "detail": "", "warnings": [],
                                        "owner": "dana"}
        onboarding._job("probe_org", "fetching", counts={"objects": 12})
        onboarding._track("probe_org", "classes", total=400, done=200)
        onboarding._track("probe_org", "flows", total=10)
        s = dana.get("/api/orgs/probe_org/status").json()
        check("a percentage is reported", isinstance(s["percent"], int) and 0 < s["percent"] < 100,
              str(s.get("percent")))
        check("the phase has a human label",
              s["step_label"] == "Fetching and analysing components (in parallel)",
              str(s.get("step_label")))
        check("the phase's position in the sequence is given",
              s["step_index"] == 3 and s["step_count"] == 5, f"{s.get('step_index')}/{s.get('step_count')}")
        check("the full phase list rides along so the UI can tick them off",
              len(s["steps"]) == 5 and s["steps"][0]["label"] == "Verifying the connection")
        check("each step says its own state",
              [st["state"] for st in s["steps"]] == ["done", "done", "active", "pending", "pending"],
              str([st.get("state") for st in s["steps"]]))
        tracks = {t["name"]: t for t in s.get("tracks", [])}
        check("per-stream progress is exposed for the parallel phase",
              tracks.get("classes", {}).get("done") == 200 and tracks["classes"]["total"] == 400,
              str(s.get("tracks")))
        before = s["percent"]
        onboarding._track("probe_org", "classes", add=200)
        after = onboarding.JOBS["probe_org"]["percent"]
        check("the bar moves as a stream completes", after > before, f"{before} -> {after}")
        check("and stays inside the fetch phase until indexing starts",
              after < onboarding._STEP_START["indexing"], str(after))
        check("live counts are exposed", s["counts"]["objects"] == 12)
        check("elapsed time is reported", s["elapsed_seconds"] is not None)
        check("the internal owner field is never leaked", "owner" not in s)

        # Percentages must be monotonic, or a bar that goes backwards tells
        # the user the fetch restarted.
        seen = [onboarding._percent(name) for name in onboarding.STEP_ORDER]
        check("phase percentages only ever increase", seen == sorted(seen), str(seen))
        check("a finished job reports 100%", onboarding._percent("done") == 100)

        # ---------- 5. concurrent fetch of the same org is refused ----------
        print("\norg fetch: a second concurrent fetch of one org is refused")
        check("a fetch is recognised as in flight", onboarding.job_in_flight("probe_org") is True)
        r = dana.post("/api/orgs", json={"org_id": "probe_org", "org_name": "Probe",
                                         "instance_url": "https://nope.invalid",
                                         "access_token": "t"})
        check("connecting it again is rejected with 409", r.status_code == 409, r.text[:200])
        check("and the refusal names the phase it is on",
              "Fetching and analysing components" in r.text, r.text[:200])

        # A refresh of the same org is the more likely collision -- two people
        # both reaching for Refresh when an org looks stale.
        reg = storage.load_registry()
        reg["probe_org"] = {"name": "Probe", "instance_url": "https://nope.invalid",
                            "owner": "dana", "visibility": "private",
                            "first_onboarded_at": iso_now(), "last_extracted_at": iso_now(),
                            "component_counts": {}, "warnings": []}
        storage.save_registry(reg)
        r = dana.post("/api/orgs/probe_org/refresh", json={"access_token": "t"})
        check("refreshing it is rejected too", r.status_code == 409, r.text[:200])

        onboarding._job("probe_org", "done")
        check("once finished, a refresh is allowed again",
              onboarding.job_in_flight("probe_org") is False)

        # ---------- 6. no shared connection at all ----------
        print("\nno LLM configured: the failure explains whose problem it is")
        saved = os.environ.pop("TS_LLM_API_KEY")
        try:
            check("the connection reports itself unconfigured",
                  llm_config.configured() is False)
            s = dana.get("/api/chat/key").json()
            check("a user is told chat is not ready", s["ready"] is False)
            check("and is not told to add a key they cannot add",
                  s["can_manage"] is False)
            r = dana.get("/api/chat/models")
            check("the model list fails with a specific reason", r.status_code == 404, r.text[:120])
            check("pointing the user at an administrator",
                  "administrator" in r.text.lower(), r.text[:200])
        finally:
            os.environ["TS_LLM_API_KEY"] = saved

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("All LLM/usage API checks passed.")
    return 0


def test_llm_and_usage_api():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
