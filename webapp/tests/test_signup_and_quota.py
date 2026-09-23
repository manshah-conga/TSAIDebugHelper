"""
End-to-end check of self-registration and LLM token quotas
(app/auth.py signup, app/limits.py, app/rate_limit.py).

Runs the real FastAPI app against a temporary data directory. The chat
enforcement case is exercised through `limits.check_turn_allowed` rather than
by streaming a real turn, because the point under test is the decision, not
the provider call -- and there is no provider in a test run.

Run:  python -m pytest tests/test_signup_and_quota.py -q
 or:  python tests/test_signup_and_quota.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-signup-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
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
from app import auth, limits, rate_limit, usage  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{status}] {label}" + (f"  {extra}" if extra and not condition else ""))


def signup(client, username, password="password123", role=None):
    body = {"username": username, "password": password}
    if role is not None:
        body["role"] = role
    return client.post("/api/auth/signup", json=body)


def main():
    with TestClient(app) as boot:
        admin = boot
        r = admin.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        assert r.status_code == 200, r.text

        print("\n-- the signup form's own config --")
        cfg = TestClient(app).get("/api/auth/signup-config")
        check("signup-config is served without a session", cfg.status_code == 200)
        check("it does not advertise the admin role",
              "admin" not in (cfg.json().get("roles") or []), str(cfg.json()))
        check("writer is the default", cfg.json().get("default_role") == "user")

        print("\n-- registering --")
        dana = TestClient(app)
        r = signup(dana, "dana")
        check("a new account can be created", r.status_code == 200, r.text[:160])
        check("the role defaults to writer", r.json().get("role") == "user", r.text[:120])
        check("signing up signs you in", dana.get("/api/auth/me").status_code == 200)
        check("the session carries the new role",
              dana.get("/api/auth/me").json().get("role") == "user")
        check("a quota is reported on the way in", bool(r.json().get("quota")), r.text[:200])

        rex = TestClient(app)
        r = signup(rex, "rex", role="reader")
        check("reader can be chosen at signup", r.json().get("role") == "reader", r.text[:120])
        check("...and it is enforced: a reader cannot write",
              rex.post("/api/orgs", json={"org_id": "x", "org_name": "x",
                                          "instance_url": "https://x.invalid",
                                          "access_token": "t"}).status_code == 403)

        print("\n-- what registration refuses --")
        # Every TestClient looks like the same source address, so the whole
        # suite shares one allowance. Reset between phases: this is a test
        # artefact, not the behaviour under test.
        rate_limit.reset()
        c = TestClient(app)
        check("the admin role cannot be requested",
              signup(c, "sneaky", role="admin").status_code == 400)
        check("...and no such account was created", auth.get_user("sneaky") is None)
        check("an unknown role is rejected outright rather than downgraded",
              signup(c, "sneaky2", role="superuser").status_code == 400)
        check("a reserved username is refused", signup(c, "administrator").status_code == 400)
        check("'admin' itself is refused", signup(c, "admin").status_code == 400)
        check("a username with a space is refused", signup(c, "da na").status_code == 400)
        check("a two-character username is refused", signup(c, "ab").status_code == 400)
        check("a short password is refused", signup(c, "shorty", password="abc").status_code == 400)
        check("a duplicate username is refused", signup(c, "dana").status_code == 400)
        r = signup(TestClient(app), "MixedCase")
        check("uppercase is normalised, not rejected",
              r.status_code == 200 and r.json()["username"] == "mixedcase", r.text[:120])

        print("\n-- provenance and tiers --")
        raw = storage.load_users()
        check("a self-registered account records that fact",
              raw["dana"].get("created_by") == "self")
        check("...and starts unverified", raw["dana"].get("verified") is False)
        check("an admin-created account records who made it",
              admin.post("/api/admin/users", json={"username": "carl",
                                                   "password": "password123",
                                                   "role": "user"}).status_code == 200
              and storage.load_users()["carl"].get("created_by") == "admin:admin")
        check("...and is verified without a second click",
              limits.tier_for(storage.load_users()["carl"]) == "verified")
        check("a self-registered account is on the unverified tier",
              limits.tier_for(storage.load_users()["dana"]) == "unverified")
        check("an account predating the field counts as verified",
              limits.tier_for({"role": "user", "created_at": "2020-01-01T00:00:00Z"}) == "verified")
        check("admins are exempt entirely",
              limits.effective_limits({"role": "admin"})["daily_tokens"] is None)

        print("\n-- the quota decision --")
        cfgdoc = limits.load_config()
        daily_cap = cfgdoc["tiers"]["unverified"]["daily_tokens"]
        allowed, reason, status = limits.check_turn_allowed("dana", auth.get_user("dana"))
        check("a fresh account may ask a question", allowed is True, str(reason))
        check("...and is told what its allowance is", status["daily"]["limit"] == daily_cap)

        # Spend the daily allowance through the real ledger, one record, so the
        # enforcement path reads exactly what a finished turn would have written.
        usage.record_turn("dana", chat_id="c1", model="m", usage={"total_tokens": daily_cap})
        allowed, reason, status = limits.check_turn_allowed("dana", auth.get_user("dana"))
        check("at the cap, the next turn is refused", allowed is False)
        check("the refusal names the window and the numbers",
              reason and "today's limit" in reason and f"{daily_cap:,}" in reason, str(reason))
        check("...and says what makes it go away",
              reason and "unverified" in reason, str(reason))

        print("\n-- verification lifts the cap --")
        r = admin.patch("/api/admin/users/dana/verified", json={"verified": True})
        check("an admin can verify an account", r.status_code == 200, r.text[:140])
        check("verification grants no new permission",
              auth.get_user("dana")["role"] == "user")
        allowed, reason, status = limits.check_turn_allowed("dana", auth.get_user("dana"))
        check("the same account may now ask again", allowed is True, str(reason))
        check("...on the verified tier", status["tier"] == "verified")
        r = admin.patch("/api/admin/users/dana/verified", json={"verified": False})
        allowed, _, _ = limits.check_turn_allowed("dana", auth.get_user("dana"))
        check("un-verifying puts the cap straight back", allowed is False)

        print("\n-- per-account overrides --")
        r = admin.patch("/api/admin/users/dana/limits",
                        json={"daily_tokens": daily_cap * 10, "monthly_tokens": daily_cap * 100})
        check("an admin can raise one account's limit", r.status_code == 200, r.text[:140])
        allowed, reason, status = limits.check_turn_allowed("dana", auth.get_user("dana"))
        check("the override wins over the tier", allowed is True and status["source"] == "override",
              str(status.get("source")))
        r = admin.patch("/api/admin/users/dana/limits", json={"daily_tokens": 0})
        check("zero is rejected rather than read as 'unlimited'", r.status_code == 400, r.text[:140])
        r = admin.patch("/api/admin/users/dana/limits", json={"clear": True})
        check("clearing the override returns the account to its tier",
              r.status_code == 200
              and limits.effective_limits(auth.get_user("dana"))["source"] == "unverified",
              r.text[:140])

        print("\n-- tier defaults are editable, not hardcoded --")
        r = admin.put("/api/admin/limits",
                      json={"tiers": {"unverified": {"daily_tokens": daily_cap * 5}}})
        check("an admin can raise the whole tier", r.status_code == 200, r.text[:140])
        check("...and it takes effect for accounts on that tier",
              limits.effective_limits(auth.get_user("dana"))["daily_tokens"] == daily_cap * 5)
        check("the monthly figure it did not send is left alone",
              limits.load_config()["tiers"]["unverified"]["monthly_tokens"]
              == cfgdoc["tiers"]["unverified"]["monthly_tokens"])
        check("who changed it is recorded", limits.load_config()["updated_by"] == "admin")
        r = admin.put("/api/admin/limits", json={"tiers": {"nosuchtier": {"daily_tokens": 5}}})
        check("an unknown tier is rejected", r.status_code == 400, r.text[:140])
        r = admin.put("/api/admin/limits", json={"tiers": {"verified": {"daily_tokens": "lots"}}})
        check("a non-numeric limit is rejected", r.status_code == 400, r.text[:140])
        r = admin.put("/api/admin/limits", json={"window_days": 9999})
        check("an absurd window is clamped, not stored",
              r.status_code == 200 and limits.load_config()["window_days"] <= 90, r.text[:140])
        r = admin.put("/api/admin/limits", json={"window_days": 30})
        check("a blank cap means unlimited",
              admin.put("/api/admin/limits",
                        json={"tiers": {"verified": {"daily_tokens": None}}}).status_code == 200
              and limits.load_config()["tiers"]["verified"]["daily_tokens"] is None)

        print("\n-- only admins may change any of it --")
        for path, method, body in (
                ("/api/admin/limits", "put", {"tiers": {}}),
                ("/api/admin/users/carl/verified", "patch", {"verified": True}),
                ("/api/admin/users/carl/limits", "patch", {"clear": True})):
            r = getattr(dana, method)(path, json=body)
            check(f"a writer cannot {method.upper()} {path}", r.status_code == 403, str(r.status_code))

        print("\n-- everyone can see their own usage --")
        r = rex.get("/api/usage/me")
        check("a reader can read their own usage", r.status_code == 200, r.text[:120])
        check("...including their quota", bool(r.json().get("quota")))
        check("a reader still cannot read everyone's",
              rex.get("/api/admin/usage").status_code == 403)
        check("the admin user list carries each account's quota",
              bool(admin.get("/api/admin/users").json()["dana"].get("quota")))

        print("\n-- registration can be switched off --")
        rate_limit.reset()
        os.environ["TS_SIGNUP_ENABLED"] = "0"
        try:
            c = TestClient(app)
            check("signup is refused with 403", signup(c, "toolate").status_code == 403)
            check("...and the form knows not to offer it",
                  c.get("/api/auth/signup-config").json().get("enabled") is False)
        finally:
            os.environ.pop("TS_SIGNUP_ENABLED", None)

        print("\n-- rate limiting --")
        rate_limit.reset()
        limit, _ = rate_limit.BUCKETS["signup"]
        c = TestClient(app)
        codes = [signup(c, f"flood{i}").status_code for i in range(limit + 2)]
        check(f"the first {limit} accounts are created", codes[:limit].count(429) == 0, str(codes))
        check("the ones past the limit are refused with 429", codes[-1] == 429, str(codes[-2:]))

        # The distinction that matters: a person fumbling the form must not
        # burn the accounts-created allowance and lock themselves out.
        rate_limit.reset()
        c = TestClient(app)
        rejected = [signup(c, "x", password="short").status_code for _ in range(limit + 2)]
        check("failed attempts do not consume the creation allowance",
              rejected.count(429) == 0, str(rejected))
        check("...and a good registration still goes through afterwards",
              signup(c, "patient").status_code == 200)
        rate_limit.reset()
        check("login is limited too (it never was before)",
              any(TestClient(app).post("/api/auth/login",
                                       json={"username": "nobody", "password": "wrong"}).status_code == 429
                  for _ in range(rate_limit.BUCKETS["login"][0] + 2)))
        rate_limit.reset()
        check("...and a good password still works afterwards",
              TestClient(app).post("/api/auth/login",
                                   json={"username": "dana", "password": "password123"}).status_code == 200)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All signup / quota checks passed.")


def test_signup_and_quota():
    main()


if __name__ == "__main__":
    main()
