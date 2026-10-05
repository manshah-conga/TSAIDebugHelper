"""
Azure OpenAI v1 endpoint support.

Azure now offers an OpenAI-compatible surface:

    POST https://<resource>.openai.azure.com/openai/v1/chat/completions
    api-key: <key>
    {"model": "<deployment>", "messages": [...]}

It differs from the deployment-scoped URL this app originally accepted in two
ways that matter: there is NO ?api-version=, and the deployment is named in the
BODY as `model` instead of in the path. The app used to reject such a URL
outright ("missing its ?api-version=... parameter") and, had it accepted one,
would have stripped `model` from the body -- so every call would have failed.

These checks run against a loopback double of Azure, so the real wire shape
(headers, body, SSE stream, error bodies) is exercised end to end, including
the admin "store personal key" route that produced the original 400.

Run:  python -m pytest tests/test_azure_v1.py -q
 or:  python tests/test_azure_v1.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ts-azure-v1-test-")
os.environ["TS_ADMIN_PASSWORD"] = "adminpassword123"
os.environ["TS_SKIP_ENV_FILE"] = "1"
for _v in ("TS_LLM_PROVIDER", "TS_LLM_API_KEY", "TS_LLM_ENDPOINT",
           "TS_LLM_DEFAULT_MODEL", "TS_LLM_LOCK_MODEL"):
    os.environ.pop(_v, None)

from app import storage  # noqa: E402

storage.DATA_ROOT = _TMP
storage.ORGS_ROOT = os.path.join(_TMP, "orgs")
storage.REGISTRY_PATH = os.path.join(_TMP, "registry.json")
storage.LOGS_ROOT = os.path.join(_TMP, "normalized_logs")
storage.AUTH_ROOT = os.path.join(_TMP, "auth")
storage.USERS_PATH = os.path.join(storage.AUTH_ROOT, "users.json")
storage.TOKENS_PATH = os.path.join(storage.AUTH_ROOT, "tokens.json")

from app import llm, llm_config, secrets_store, chat_store  # noqa: E402

secrets_store.LLM_KEYS_PATH = os.path.join(storage.AUTH_ROOT, "llm_keys.json")
chat_store.CHATS_ROOT = os.path.join(_TMP, "chats")
chat_store.SHARES_PATH = os.path.join(chat_store.CHATS_ROOT, "_shares.json")
secrets_store.KEK_ROUNDS = 1000

FAILURES = []
KEY = "azure-test-key-0123456789abcdef"
GOOD_MODEL = "gpt-6-luna"
REASONER = "reasoner-x"       # rejects max_tokens and temperature, like o-series
# Behaves exactly as gpt-6-luna was observed to on 2026-10-05: refuses function
# tools on /chat/completions while reasoning, ALSO refuses reasoning_effort
# "none", and works only through /v1/responses.
LUNA = "luna-like"
LUNA_ERROR = ("Function tools with reasoning_effort are not supported for gpt-6-luna in "
              "/v1/chat/completions. To use function tools, use /v1/responses or set "
              "reasoning_effort to 'none'.")
LUNA_NONE_ERROR = ("Unsupported value: 'reasoning_effort' does not support 'none' with this "
                   "model. Supported values are: 'low', 'medium', 'high', and 'xhigh'.")


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------- Azure double

REQUESTS = []


class FakeAzure(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        REQUESTS.append({"path": self.path, "headers": dict(self.headers), "body": body})

        if self.headers.get("api-key") != KEY:
            return self._send(401, {"error": {"code": "401", "message": "Access denied"}})
        path = self.path.split("?")[0]
        if path.endswith("/openai/v1/responses"):
            return self._responses(body)
        if not path.endswith("/openai/v1/chat/completions"):
            return self._send(404, {"error": {"code": "404", "message": "Resource not found"}})

        model = body.get("model")
        if model == LUNA and body.get("tools"):
            msg = LUNA_NONE_ERROR if body.get("reasoning_effort") == "none" else LUNA_ERROR
            return self._send(400, {"error": {"code": "invalid_request_error",
                                              "param": "reasoning_effort", "message": msg}})
        if model not in (GOOD_MODEL, REASONER, LUNA):
            return self._send(404, {"error": {"code": "DeploymentNotFound",
                                              "message": "The API deployment for this resource "
                                                         "does not exist."}})
        if model == REASONER:
            if "max_tokens" in body:
                return self._send(400, {"error": {
                    "code": "unsupported_parameter", "param": "max_tokens",
                    "message": "Unsupported parameter: 'max_tokens' is not supported with this "
                               "model. Use 'max_completion_tokens' instead."}})
            if "temperature" in body:
                return self._send(400, {"error": {
                    "code": "unsupported_value", "param": "temperature",
                    "message": "Unsupported value: 'temperature' does not support 0.2 with this "
                               "model. Only the default (1) value is supported."}})

        if not body.get("stream"):
            return self._send(200, {"choices": [{"index": 0, "finish_reason": "length",
                                                 "message": {"role": "assistant",
                                                             "content": "p"}}],
                                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                              "total_tokens": 2}})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "A 429 "}}]},
            {"choices": [{"index": 0, "delta": {"content": "is rate limiting."}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 6,
                                      "total_tokens": 18}},
        ]
        for ch in chunks:
            self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


    def _responses(self, body):
        """A strict-ish /v1/responses: rejects the mistakes a careless
        translation would make, so passing here means the shape is right."""
        def bad(msg):
            return self._send(400, {"error": {"code": "invalid_request_error", "message": msg}})

        if body.get("model") != LUNA:
            return self._send(404, {"error": {"code": "DeploymentNotFound", "message": "no"}})
        if "messages" in body or not isinstance(body.get("input"), list):
            return bad("Missing required parameter: 'input'.")
        if "temperature" in body:
            return bad("Unsupported parameter: 'temperature' is not supported with this model.")
        if "max_tokens" in body:
            return bad("Unsupported parameter: 'max_tokens'.")
        for t in body.get("tools") or []:
            if "function" in t or not t.get("name"):
                return bad("Missing required parameter: 'tools[0].name'.")
            if t.get("strict") is not False:
                return bad("Invalid schema for function: strict mode requires "
                           "additionalProperties false.")
        calls = {i.get("call_id") for i in body["input"] if i.get("type") == "function_call"}
        outs = [i for i in body["input"] if i.get("type") == "function_call_output"]
        for o in outs:
            if o.get("call_id") not in calls:
                return bad(f"No tool call found for function call output with call_id "
                           f"{o.get('call_id')}.")
        if any(i.get("role") in ("system", "tool") for i in body["input"]):
            return bad("Invalid role in input.")

        last_user = next((i.get("content") for i in reversed(body["input"])
                          if i.get("role") == "user"), "")
        want_tool = bool(body.get("tools")) and "use tool" in (last_user or "") and not outs
        usage = {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49,
                 "output_tokens_details": {"reasoning_tokens": 5}}

        if not body.get("stream"):
            return self._send(200, {"id": "resp_1", "status": "completed",
                                    "output": [{"type": "message", "role": "assistant",
                                                "content": [{"type": "output_text",
                                                             "text": "ok"}]}],
                                    "usage": usage})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def ev(obj):
            self.wfile.write(f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode())

        ev({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
        ev({"type": "response.reasoning_summary_text.delta", "delta": "thinking..."})
        if want_tool:
            item = {"type": "function_call", "id": "fc_1", "call_id": "call_abc",
                    "name": "get_org_stats", "arguments": ""}
            ev({"type": "response.output_item.added", "output_index": 1, "item": item})
            ev({"type": "response.function_call_arguments.delta", "item_id": "fc_1",
                "output_index": 1, "delta": '{"org_id": '})
            ev({"type": "response.function_call_arguments.delta", "item_id": "fc_1",
                "output_index": 1, "delta": '"acme"}'})
            ev({"type": "response.output_item.done", "output_index": 1,
                "item": dict(item, arguments='{"org_id": "acme"}')})
        else:
            answer = ("The org has 12 triggers." if outs else "A 429 is rate limiting.")
            for piece in (answer[:6], answer[6:]):
                ev({"type": "response.output_text.delta", "item_id": "msg_1",
                    "output_index": 1, "delta": piece})
        ev({"type": "response.completed",
            "response": {"id": "resp_1", "status": "completed", "usage": usage}})


def start_fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAzure)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------- tests

def test_url_validation():
    print("\nvalidation: both Azure URL styles")
    v1 = "https://conga-ts.openai.azure.com/openai/v1/chat/completions"
    check("a v1 URL without api-version is accepted",
          llm.validate_azure_endpoint(v1) == v1)
    check("a v1 URL with ?api-version=preview is also accepted",
          llm.validate_azure_endpoint(v1 + "?api-version=preview").endswith("api-version=preview"))
    check("a v1 BASE url is completed to chat/completions",
          llm.validate_azure_endpoint("https://conga-ts.openai.azure.com/openai/v1/") == v1,
          llm.validate_azure_endpoint("https://conga-ts.openai.azure.com/openai/v1/"))
    check("is_azure_v1 recognises v1", llm.is_azure_v1(v1))

    legacy = ("https://r.openai.azure.com/openai/deployments/gpt-4o/chat/completions"
              "?api-version=2024-08-01-preview")
    check("a legacy deployment URL still validates", llm.validate_azure_endpoint(legacy) == legacy)
    check("and is not mistaken for v1", not llm.is_azure_v1(legacy))
    try:
        llm.validate_azure_endpoint(legacy.split("?")[0])
        check("a legacy URL without api-version is still rejected", False)
    except ValueError as e:
        check("a legacy URL without api-version is still rejected", "api-version" in str(e))
        check("and the message points at the v1 alternative", "/openai/v1/" in str(e), str(e))

    try:
        llm.validate_azure_endpoint("https://r.openai.azure.com/openai/v1/embeddings")
        check("a v1 URL that is not chat completions is rejected", False)
    except ValueError:
        check("a v1 URL that is not chat completions is rejected", True)

    try:
        llm.validate_azure_model(v1, "")
        check("a v1 URL with no model is rejected", False)
    except ValueError as e:
        check("a v1 URL with no model is rejected", "model" in str(e).lower())
    check("a v1 URL with a model passes", llm.validate_azure_model(v1, " gpt-6-luna ") == "gpt-6-luna")
    check("a legacy URL needs no model", llm.validate_azure_model(legacy, None) is None)
    check("azure_model: v1 uses the model name", llm.azure_model(v1, "gpt-6-luna") == "gpt-6-luna")
    check("azure_model: legacy uses the path", llm.azure_model(legacy, "ignored") == "gpt-4o")


def test_prepare_body():
    print("\nwire: what is sent")
    v1 = llm.creds("azure", KEY, "https://x.openai.azure.com/openai/v1/chat/completions",
                   model="gpt-6-luna")
    url, headers, body = llm._prepare(v1, {"model": "something-else", "messages": [],
                                           "stream": True, "usage": {"include": True}})
    check("v1: model is SET to the configured deployment", body.get("model") == "gpt-6-luna",
          str(body.get("model")))
    check("v1: URL is sent unchanged", url.endswith("/openai/v1/chat/completions"))
    check("v1: api-key header", headers.get("api-key") == KEY and "Authorization" not in headers)
    check("v1: OpenRouter usage extension stripped", "usage" not in body)
    check("v1: stream_options set", body.get("stream_options") == {"include_usage": True})

    legacy = llm.creds("azure", KEY, "https://x.openai.azure.com/openai/deployments/d/"
                                     "chat/completions?api-version=2024-08-01-preview")
    _, _, body = llm._prepare(legacy, {"model": "m", "messages": []})
    check("legacy: model still stripped", "model" not in body)

    adj = llm._azure_retry_body({"max_tokens": 5, "temperature": 0.2}, 400,
                                "Unsupported parameter: 'max_tokens' ... use "
                                "'max_completion_tokens' instead.")
    check("retry: max_tokens becomes max_completion_tokens",
          adj == {"max_completion_tokens": 5, "temperature": 0.2}, str(adj))
    check("retry: an unrelated 400 is not retried",
          llm._azure_retry_body({"max_tokens": 5}, 400, "content filter") is None)


def test_live_calls(base):
    print("\nlive: verify + stream against a loopback Azure double")
    url = f"{base}/openai/v1/chat/completions"

    REQUESTS.clear()
    ok = asyncio.run(llm.verify_creds(llm.creds("azure", KEY, url, model=GOOD_MODEL)))
    check("verify_creds succeeds on a v1 endpoint", ok is True)
    check("and sent model in the body", REQUESTS[-1]["body"].get("model") == GOOD_MODEL)
    check("and no api-version was needed", "api-version" not in REQUESTS[-1]["path"])

    try:
        asyncio.run(llm.verify_creds(llm.creds("azure", KEY, url, model="nope")))
        check("a wrong deployment name is reported", False)
    except llm.LLMError as e:
        check("a wrong deployment name is reported as no_model", e.code == "no_model", e.code)

    try:
        asyncio.run(llm.verify_creds(llm.creds("azure", "wrong", url, model=GOOD_MODEL)))
        check("a wrong key is reported", False)
    except llm.LLMError as e:
        check("a wrong key is reported as bad_key", e.code == "bad_key", e.code)

    REQUESTS.clear()
    ok = asyncio.run(llm.verify_creds(llm.creds("azure", KEY, url, model=REASONER)))
    check("a reasoning deployment verifies after one compatibility retry", ok is True)
    check("the retry used max_completion_tokens",
          "max_completion_tokens" in REQUESTS[-1]["body"] and
          "max_tokens" not in REQUESTS[-1]["body"], str(REQUESTS[-1]["body"]))

    async def collect(model):
        c = llm.creds("azure", KEY, url, model=model)
        text, usage = "", None
        async for d in llm.stream_chat(c, "picker-value", [{"role": "user", "content": "429?"}]):
            text += d.content or ""
            usage = d.usage or usage
        return text, usage

    REQUESTS.clear()
    text, usage = asyncio.run(collect(GOOD_MODEL))
    check("streaming works on v1", text == "A 429 is rate limiting.", repr(text))
    check("usage arrives", (usage or {}).get("total_tokens") == 18, str(usage))
    check("the connection's model overrides the picker value",
          REQUESTS[-1]["body"].get("model") == GOOD_MODEL)

    llm._AZURE_QUIRKS.clear()
    REQUESTS.clear()
    text, _ = asyncio.run(collect(REASONER))
    check("streaming on a reasoning deployment survives the temperature 400",
          text == "A 429 is rate limiting.", repr(text))
    check("by resending without temperature", "temperature" not in REQUESTS[-1]["body"])

    print("\nlive: gpt-6-luna-like deployment -> switched to the Responses API")
    llm._AZURE_QUIRKS.clear()
    REQUESTS.clear()
    c_luna = llm.creds("azure", KEY, url, model=LUNA)
    ok = asyncio.run(llm.verify_creds(c_luna))
    check("the save-time test sends a tool, so it meets the real failure",
          REQUESTS and REQUESTS[0]["body"].get("tools"))
    check("verify succeeds (was: 'does not support none')", ok is True)
    check("by switching to /v1/responses", REQUESTS[-1]["path"].endswith("/openai/v1/responses"),
          REQUESTS[-1]["path"])
    check("without wasting a call on reasoning_effort=none",
          not any(r["body"].get("reasoning_effort") == "none" for r in REQUESTS))
    check("the switch is remembered for this deployment", llm.uses_responses_api(c_luna))

    tools = [{"type": "function", "function": {
        "name": "get_org_stats", "description": "Counts for an org.",
        "parameters": {"type": "object", "properties": {"org_id": {"type": "string"}}}}}]
    convo = [{"role": "system", "content": "You are the debug helper."},
             {"role": "user", "content": "use tool to count triggers"}]

    async def run_round(messages):
        text, reasoning, acc, finish, usage = "", "", {}, None, None
        async for d in llm.stream_chat(c_luna, "picker", messages, tools=tools):
            text += d.content or ""
            reasoning += d.reasoning or ""
            if d.tool_calls:
                llm.accumulate_tool_calls(acc, d.tool_calls)
            finish = d.finish_reason or finish
            usage = d.usage or usage
        return text, reasoning, llm.finalize_tool_calls(acc), finish, usage

    REQUESTS.clear()
    text, reasoning, calls, finish, usage = asyncio.run(run_round(convo))
    check("round 1 goes straight to /responses (learned at save time)",
          len(REQUESTS) == 1 and REQUESTS[0]["path"].endswith("/responses"), str(len(REQUESTS)))
    sent = REQUESTS[0]["body"]
    check("system prompt sent as a developer message",
          sent["input"][0] == {"role": "developer", "content": "You are the debug helper."})
    check("tools flattened with strict=False",
          sent["tools"][0]["name"] == "get_org_stats" and sent["tools"][0]["strict"] is False)
    check("store=False, no temperature", sent.get("store") is False and "temperature" not in sent)
    check("the tool call comes back whole",
          len(calls) == 1 and calls[0]["id"] == "call_abc" and calls[0]["name"] == "get_org_stats"
          and calls[0]["args"] == {"org_id": "acme"}, str(calls))
    check("finish_reason is tool_calls", finish == "tool_calls", str(finish))
    check("reasoning summary streams to the dock", reasoning == "thinking...", repr(reasoning))
    check("usage mapped to the chat shape",
          (usage or {}).get("prompt_tokens") == 40 and
          usage["completion_tokens_details"]["reasoning_tokens"] == 5, str(usage))

    # Round 2: exactly what chat.to_provider_messages produces after a tool ran.
    convo2 = convo + [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_abc", "type": "function",
            "function": {"name": "get_org_stats", "arguments": '{"org_id": "acme"}'}}]},
        {"role": "tool", "tool_call_id": "call_abc", "name": "get_org_stats",
         "content": '{"triggers": 12}'},
        {"role": "system", "content": "You have 2 tool call round(s) left."},
    ]
    REQUESTS.clear()
    text, _, calls, finish, _ = asyncio.run(run_round(convo2))
    check("round 2 (after the tool result) answers in text",
          text == "The org has 12 triggers." and not calls and finish == "stop",
          f"{text!r} {calls} {finish}")
    kinds = [i.get("type") or i.get("role") for i in REQUESTS[0]["body"]["input"]]
    check("the tool call and its output are paired by call_id",
          kinds == ["developer", "user", "function_call", "function_call_output", "developer"],
          str(kinds))

    async def plain():
        out = ""
        async for d in llm.stream_chat(c_luna, "x", [{"role": "user", "content": "hi"}]):
            out += d.content or ""
        return out
    check("a no-tools call (force_answer) also works", asyncio.run(plain()) ==
          "A 429 is rate limiting.")

    llm._AZURE_QUIRKS.clear()           # e.g. after a restart
    REQUESTS.clear()
    text, _, calls, _, _ = asyncio.run(run_round(convo))
    check("with nothing learned yet, a chat round switches after one rejected request",
          len(calls) == 1 and len(REQUESTS) == 2 and REQUESTS[1]["path"].endswith("/responses"),
          f"{len(REQUESTS)} requests")

    print("\nunit: the reasoning_effort decision")
    legacy = llm.creds("azure", KEY, "https://r.openai.azure.com/openai/deployments/d/"
                                     "chat/completions?api-version=2024-08-01-preview")
    llm._AZURE_QUIRKS.clear()
    adj = llm._azure_retry_body({"tools": [1]}, 400, LUNA_ERROR, legacy)
    check("legacy URL: falls back to reasoning_effort=none",
          adj is not None and adj.get("reasoning_effort") == "none", str(adj))
    check("legacy URL: and gives up if 'none' is refused too",
          llm._azure_retry_body({"tools": [1], "reasoning_effort": "none"}, 400,
                                LUNA_NONE_ERROR, legacy) is None)
    check("a normal deployment never gets reasoning_effort",
          "reasoning_effort" not in llm._prepare(
              llm.creds("azure", KEY, url, model=GOOD_MODEL), {"messages": []})[2])


def test_shared_env(base):
    print("\nshared connection: v1 from the environment")
    os.environ["TS_LLM_PROVIDER"] = "azure"
    os.environ["TS_LLM_API_KEY"] = KEY
    os.environ["TS_LLM_ENDPOINT"] = f"{base}/openai/v1/"
    os.environ.pop("TS_LLM_DEFAULT_MODEL", None)
    check("v1 with no TS_LLM_DEFAULT_MODEL is not usable", llm_config.configured() is False)
    err = llm_config.public_state()["config_error"] or ""
    check("and says to set TS_LLM_DEFAULT_MODEL", "TS_LLM_DEFAULT_MODEL" in err, err)

    os.environ["TS_LLM_DEFAULT_MODEL"] = GOOD_MODEL
    check("with the model set it is usable", llm_config.configured() is True,
          str(llm_config.public_state()["config_error"]))
    c = llm_config.creds()
    check("creds carry the model", c["model"] == GOOD_MODEL)
    check("the base URL was completed", c["endpoint"].endswith("/openai/v1/chat/completions"))
    check("default model is the deployment", llm_config.default_model() == GOOD_MODEL)
    check("startup report names it", GOOD_MODEL in llm_config.startup_report())
    models = asyncio.run(llm.list_models_for(c))
    check("the picker shows the deployment", models[0]["id"] == GOOD_MODEL)
    for v in ("TS_LLM_PROVIDER", "TS_LLM_API_KEY", "TS_LLM_ENDPOINT", "TS_LLM_DEFAULT_MODEL"):
        os.environ.pop(v, None)


def test_store_key_route(base):
    """The exact path that returned the original 400."""
    print("\nroute: POST /api/chat/key with a v1 endpoint")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "adminpassword123"})
        if r.status_code != 200:
            check("admin can log in", False, r.text)
            return
        url = f"{base}/openai/v1/chat/completions"

        r = c.post("/api/chat/key", json={"api_key": KEY, "password": "adminpassword123",
                                          "provider": "azure", "endpoint": url})
        check("v1 without a model is refused with a clear message",
              r.status_code == 400 and "model" in r.text.lower(), r.text)
        check("and NOT the old api-version complaint", "api-version" not in r.text, r.text)

        r = c.post("/api/chat/key", json={"api_key": KEY, "password": "adminpassword123",
                                          "provider": "azure", "endpoint": url,
                                          "model": GOOD_MODEL})
        check("v1 with a model is stored", r.status_code == 200, r.text)
        if r.status_code == 200:
            s = r.json()
            check("the admin is now on the personal connection", s["using"] == "personal")
            check("the default model is the deployment", s["default_model"] == GOOD_MODEL,
                  str(s["default_model"]))
            check("the personal state shows the model back", s["personal"]["model"] == GOOD_MODEL)

        r = c.get("/api/chat/models")
        check("the model list shows the deployment",
              r.status_code == 200 and r.json()["models"][0]["id"] == GOOD_MODEL, r.text[:300])


def main():
    srv, base = start_fake()
    try:
        test_url_validation()
        test_prepare_body()
        test_live_calls(base)
        test_shared_env(base)
        test_store_key_route(base)
    finally:
        srv.shutdown()
        shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


def test_all():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
