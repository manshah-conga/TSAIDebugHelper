"""
Session cookie Secure attribute (TS_COOKIE_SECURE).

  auto (default) -- Secure only when the request arrived over HTTPS, either
                    directly (uvicorn --ssl-*) or via X-Forwarded-Proto from a
                    trusted proxy. Plain http:// keeps working.
  1 / 0          -- force on / off.

Run:  python tests/test_cookie_secure.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-cookie-test-")
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

FAILURES = []


def check(label, condition, extra=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _login_cookie_header(base_url="http://testserver", headers=None, env=None):
    saved = {k: os.environ.get(k) for k in ("TS_COOKIE_SECURE", "TS_TRUST_FORWARDED_FOR")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            os.environ[k] = v
        c = TestClient(app, base_url=base_url)
        r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"},
                   headers=headers or {})
        assert r.status_code == 200, r.text
        sc = r.headers.get("set-cookie", "")
        out = c.post("/api/auth/logout", headers=headers or {})
        return sc, out.headers.get("set-cookie", "")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _secure(sc):
    return "secure" in [p.strip().lower() for p in sc.split(";")]


def main():
    print("\n-- session cookie Secure attribute --")
    # Run the lifespan once so the admin account is bootstrapped; the MCP
    # session manager it starts can only run once per process.
    with TestClient(app):
        pass
    sc, lo = _login_cookie_header()
    check("plain http (today's VM): no Secure, login still works", "ts_session=" in sc and not _secure(sc), sc)
    check("plain http: logout cookie not Secure either", not _secure(lo), lo)
    check("HttpOnly and SameSite=lax are kept", "httponly" in sc.lower() and "samesite=lax" in sc.lower(), sc)

    sc, lo = _login_cookie_header(base_url="https://testserver")
    check("direct HTTPS (uvicorn --ssl-*): Secure", _secure(sc), sc)
    check("direct HTTPS: logout cookie Secure too", _secure(lo), lo)

    sc, _ = _login_cookie_header(headers={"X-Forwarded-Proto": "https"})
    check("behind a proxy sending X-Forwarded-Proto: https -> Secure", _secure(sc), sc)

    sc, _ = _login_cookie_header(headers={"X-Forwarded-Proto": "https"},
                                 env={"TS_TRUST_FORWARDED_FOR": "0"})
    check("X-Forwarded-Proto ignored when forwarded headers are not trusted", not _secure(sc), sc)

    sc, _ = _login_cookie_header(env={"TS_COOKIE_SECURE": "1"})
    check("TS_COOKIE_SECURE=1 forces Secure even on http", _secure(sc), sc)

    sc, _ = _login_cookie_header(base_url="https://testserver", env={"TS_COOKIE_SECURE": "0"})
    check("TS_COOKIE_SECURE=0 forces it off even on https", not _secure(sc), sc)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("All cookie Secure checks passed.")
    return 0


def test_cookie_secure():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
