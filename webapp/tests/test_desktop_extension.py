"""
Claude Desktop extension download (app/desktop_ext.py, /api/desktop-extension*).

Run:  python tests/test_desktop_extension.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-mcpb-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
os.environ["TS_SKIP_ENV_FILE"] = "1"
os.environ.pop("TS_MCPB_PATH", None)

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
from app import auth, desktop_ext  # noqa: E402

FAILURES = []


def check(label, condition, extra=""):
    if not condition:
        FAILURES.append(f"{label} {extra}")
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra and not condition else ""))


def login(u, p):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
    return c


def make_mcpb(path, version="9.9.9"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"name": "ts-debug-helper", "display_name": "TS Debug Helper",
                                                "version": version}))
        z.writestr("server/index.js", "// bridge")
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return buf.getvalue()


def main():
    with TestClient(app):   # runs lifespan -> bootstrap admin
        pass
    auth.create_user("reader1", "readerpassword1", "reader", created_by="admin", verified=True)
    admin = login("admin", "adminpassword123")
    reader = login("reader1", "readerpassword1")
    anon = TestClient(app)

    missing = os.path.join(_TMP, "nope", "ts-debug-helper.mcpb")
    os.environ["TS_MCPB_PATH"] = missing

    print("\n-- auth --")
    check("info needs sign-in", anon.get("/api/desktop-extension").status_code == 401)
    check("download needs sign-in", anon.get("/api/desktop-extension/download").status_code == 401)

    print("\n-- not published --")
    r = reader.get("/api/desktop-extension").json()
    check("reader: unavailable", r["available"] is False)
    check("reader: no server paths leaked", "looked_in" not in r and "path" not in r)
    a = admin.get("/api/desktop-extension").json()
    check("admin: sees where it looked", a.get("looked_in") == [os.path.abspath(missing)], str(a))
    check("admin: configured flag", a.get("configured") is True)
    check("download 404 when missing", reader.get("/api/desktop-extension/download").status_code == 404)

    print("\n-- invalid file --")
    os.makedirs(os.path.dirname(missing), exist_ok=True)
    with open(missing, "wb") as f:
        f.write(b"not a zip")
    a = admin.get("/api/desktop-extension").json()
    check("invalid: unavailable", a["available"] is False)
    check("invalid: error explains", "zip" in (a.get("error") or ""), str(a))
    check("invalid: not served", reader.get("/api/desktop-extension/download").status_code == 404)

    print("\n-- published --")
    raw = make_mcpb(missing, "1.2.3")
    r = reader.get("/api/desktop-extension").json()
    check("available", r["available"] is True, str(r))
    check("version from manifest", r.get("version") == "1.2.3")
    check("size", r.get("size") == len(raw))
    d = reader.get("/api/desktop-extension/download")
    check("download 200", d.status_code == 200)
    check("download bytes", d.content == raw)
    check("attachment filename", 'filename="ts-debug-helper.mcpb"' in d.headers.get("content-disposition", ""),
          d.headers.get("content-disposition", ""))

    print("\n-- replaced without restart --")
    os.utime(missing, (1, 1))   # force a different mtime even within the same tick
    make_mcpb(missing, "1.2.4")
    check("new version picked up", reader.get("/api/desktop-extension").json().get("version") == "1.2.4")

    print("\n-- default search order --")
    os.environ.pop("TS_MCPB_PATH", None)
    check("defaults: downloads/ first",
          desktop_ext.candidate_paths()[0].endswith(os.path.join("webapp", "downloads", "ts-debug-helper.mcpb")))
    check("defaults: desktop-extension/ second",
          desktop_ext.candidate_paths()[1].endswith(os.path.join("desktop-extension", "ts-debug-helper.mcpb")))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
