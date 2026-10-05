"""
OpenRouter client: streaming chat completions and the model catalogue.

OpenRouter speaks the OpenAI chat-completions dialect, so this is a thin
httpx wrapper rather than a vendor SDK -- httpx is already a dependency and
adding a Node runtime for @openrouter/sdk would double this app's deployment
surface for no functional gain. The SDK's `openrouter.chat.send({stream:true})`
is exactly `POST /api/v1/chat/completions` with `"stream": true`, which is
what `stream_chat` below does.

What this module does NOT do: it has no idea about tools, MCP, incidents, or
the agent loop. It streams deltas and yields them. The loop lives in chat.py.
"""
import json
import os
import re
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import httpx

OPENROUTER_BASE = os.environ.get("TS_OPENROUTER_URL", "https://openrouter.ai/api/v1").rstrip("/")

# Providers. Azure OpenAI speaks the same chat-completions dialect, so the agent
# loop is untouched -- but four things differ at the wire, and all four are
# handled in _prepare() below rather than scattered through the module:
#
#   1. Auth is `api-key: <key>`, not `Authorization: Bearer <key>`.
#   2. Which model answers depends on the URL style (see below).
#   3. The endpoint is a complete URL, so nothing may be appended to it.
#   4. Streaming usage needs `stream_options: {include_usage: true}`; the
#      `usage: {include: true}` form is an OpenRouter extension, and Azure has
#      no cost field at all (it bills on your Azure subscription, not per call).
#
# Azure has two URL styles, and both are accepted:
#
#   LEGACY  .../openai/deployments/<deployment>/chat/completions?api-version=...
#           The deployment in the PATH picks the model, so a `model` field in
#           the body is meaningless and is stripped. api-version is mandatory.
#
#   V1      .../openai/v1/chat/completions
#           The OpenAI-compatible surface. No deployment in the path and no
#           api-version; the deployment name travels as `model` in the BODY,
#           exactly as on api.openai.com. So a v1 connection must carry a model
#           name alongside the URL, and _prepare() sets it rather than strips it.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_AZURE = "azure"

# Sent on every call so usage shows up attributed in the OpenRouter dashboard.
APP_TITLE = "TS Intelligent Debug Helper"
APP_REFERER = os.environ.get("TS_PUBLIC_URL", "https://github.com/conga/ts-debug-helper")

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 180.0

_MODEL_CACHE = {"at": 0.0, "models": None}
MODEL_CACHE_TTL = 3600.0


class LLMError(Exception):
    """A provider-side failure with a message worth showing the user verbatim."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code or "llm_error"


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
    }


def creds(provider, api_key, endpoint=None, model=None):
    """The credential bundle every call takes. One shape for both providers so
    nothing above this module has to branch on which one is in use.

    `model` matters only for an Azure v1 endpoint, where it is the deployment
    name sent in the body. Elsewhere it is carried but unused."""
    return {"provider": (provider or PROVIDER_OPENROUTER).strip().lower(),
            "api_key": api_key, "endpoint": (endpoint or "").strip(),
            "model": (model or "").strip() or None}


_V1_PATH_RE = re.compile(r"/openai/v1(/|$)", re.IGNORECASE)


def is_azure_v1(url):
    """True for the OpenAI-compatible `/openai/v1/...` surface, where the
    deployment is named in the body rather than the path."""
    return bool(_V1_PATH_RE.search(urlparse((url or "").strip()).path or ""))


def normalize_azure_endpoint(url):
    """Complete a v1 base URL to its chat-completions route.

    The portal and Microsoft's samples show the v1 surface as a BASE URL
    (`.../openai/v1/`) handed to an OpenAI client, which appends the route
    itself. People paste exactly that, so accept it rather than reject it.
    Legacy URLs are returned unchanged -- there is no unambiguous fix for a
    deployment URL missing pieces."""
    url = (url or "").strip()
    if not is_azure_v1(url):
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.lower().endswith("/openai/v1"):
        path += "/chat/completions"
        return parsed._replace(path=path).geturl()
    return url


def validate_azure_endpoint(url):
    """Azure endpoints are pasted by hand from the portal, so the failure modes
    are predictable -- the resource root without the deployment path, or a
    missing api-version on a legacy URL. Catching them here turns a baffling
    404 much later into a specific message at the moment of typing.

    Returns the endpoint normalized (a v1 base URL gains /chat/completions)."""
    url = normalize_azure_endpoint(url)
    if not url:
        raise ValueError("An Azure endpoint URL is required.")
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("That does not look like a URL.")
    # https everywhere except loopback. The exception exists because the key
    # travels in a header on every call, so plaintext to a remote host would
    # put it on the wire -- but a local gateway or a test double never leaves
    # the machine, and forbidding those makes the feature untestable.
    host = (parsed.hostname or "").lower()
    is_loopback = host in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ValueError("The endpoint must be an https:// URL (http is allowed only for "
                         "localhost, for a local gateway or a test double).")

    if is_azure_v1(url):
        # v1: api-version is optional (Azure accepts `?api-version=preview` for
        # preview features, nothing otherwise), so it is neither required nor
        # stripped.
        if not parsed.path.rstrip("/").lower().endswith("/chat/completions"):
            raise ValueError(
                "That v1 URL does not point at chat completions -- it should look like "
                "https://<resource>.openai.azure.com/openai/v1/chat/completions")
        return url

    if "/chat/completions" not in parsed.path:
        raise ValueError(
            "Use the full chat completions URL, not just the resource root. Either form works: "
            "https://<resource>.openai.azure.com/openai/v1/chat/completions (v1, model name "
            "sent separately) or https://<resource>.openai.azure.com/openai/deployments/"
            "<deployment>/chat/completions?api-version=2024-08-01-preview")
    if "api-version=" not in (parsed.query or ""):
        raise ValueError(
            "This deployment-style URL is missing its ?api-version=... parameter. Add one "
            "(e.g. ?api-version=2024-08-01-preview), or use the v1 form "
            "https://<resource>.openai.azure.com/openai/v1/chat/completions, which needs none.")
    return url


def validate_azure_model(url, model):
    """A v1 endpoint names no deployment, so without a model name every call
    400s. Legacy URLs ignore it. Returns the cleaned model name or None."""
    model = (model or "").strip() or None
    if is_azure_v1(url) and not model:
        raise ValueError(
            "This is an Azure v1 endpoint (/openai/v1/...), which takes the deployment name "
            "in the request rather than the URL -- so a model / deployment name is required "
            "(for example gpt-6-luna).")
    if model and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
        raise ValueError("That model / deployment name contains characters Azure does not allow.")
    return model


def azure_deployment(url):
    """The deployment name out of a legacy URL path -- that IS the model on
    Azure, so it is what the picker shows."""
    m = re.search(r"/deployments/([^/?]+)", url or "")
    return m.group(1) if m else "azure-deployment"


def azure_model(url, model=None):
    """The model an Azure connection actually reaches: the explicit name on a
    v1 endpoint, the deployment in the path on a legacy one."""
    if is_azure_v1(url):
        return (model or "").strip() or "azure-deployment"
    return azure_deployment(url)


def _prepare(c, payload):
    """Per-provider URL, headers and body. The only place the two differ."""
    if c["provider"] == PROVIDER_AZURE:
        body = dict(payload)
        if is_azure_v1(c["endpoint"]):
            # The connection's own model wins over whatever the picker sent:
            # on Azure the connection decides the deployment, and the picker
            # only ever offers that one entry anyway.
            body["model"] = c.get("model") or body.get("model")
        else:
            body.pop("model", None)   # the deployment in the URL decides this
        body.pop("usage", None)       # OpenRouter-only extension
        if body.get("stream"):
            body["stream_options"] = {"include_usage": True}
        return c["endpoint"], {"api-key": c["api_key"], "Content-Type": "application/json"}, body
    return f"{OPENROUTER_BASE}/chat/completions", _headers(c["api_key"]), payload


# Compatibility quirks learned per Azure deployment, keyed (endpoint, model).
# Each is discovered from a 400 that names the offending parameter, applied
# from then on to every request to that deployment, and forgotten on restart
# (re-learning costs one rejected request). In memory only: nothing secret,
# and nothing worth persisting.
#
#   rename_max_tokens   send max_completion_tokens instead of max_tokens
#   drop_temperature    the model accepts only its default temperature
#   reasoning_none      send reasoning_effort="none" (legacy endpoints only).
#                       Some reasoning-by-default deployments refuse function
#                       tools on /chat/completions unless reasoning is off --
#                       and every chat round here carries tools.
#   use_responses       v1 endpoints: drive this deployment through the
#                       Responses API instead (gpt-6-luna: refuses tools while
#                       reasoning AND refuses reasoning_effort="none").
_AZURE_QUIRKS = {}
MAX_AZURE_ATTEMPTS = 4      # first try + one retry per quirk


def _quirk_key(c):
    return (c.get("endpoint") or "", c.get("model") or "")


def _apply_quirks(c, body):
    q = _AZURE_QUIRKS.get(_quirk_key(c)) or set()
    if not q:
        return body
    out = dict(body)
    if "rename_max_tokens" in q and "max_tokens" in out:
        out["max_completion_tokens"] = out.pop("max_tokens")
    if "drop_temperature" in q:
        out.pop("temperature", None)
    if "reasoning_none" in q:
        out["reasoning_effort"] = "none"
    return out


def _azure_retry_body(body, status, body_text, c=None):
    """Adjust a request after a 400 that names a parameter this deployment's
    model family does not accept, so the operator does not need to know which
    family a deployment belongs to. Returns the adjusted body, or None when
    the 400 was about something else (or the fix was already applied, so a
    retry would only repeat the failure). With `c`, the quirk is remembered
    for that deployment so later requests get it right first time."""
    if status != 400:
        return None
    text = (body_text or "").lower()
    out = dict(body)
    learned = set()

    # Tools + reasoning refused on /chat/completions. On a v1 endpoint, go
    # straight to the Responses API the error recommends: it keeps the model's
    # reasoning, where reasoning_effort="none" would throw it away (and some
    # deployments refuse "none" anyway). Elsewhere there is no Responses route
    # to switch to, so "none" is the only remedy left to try.
    v1 = c is not None and is_azure_v1(c.get("endpoint"))
    refuses_none = ("reasoning_effort" in text and out.get("reasoning_effort") == "none"
                    and ("does not support" in text or "unsupported" in text))
    if v1 and not uses_responses_api(c) and (
            refuses_none or ("reasoning_effort" in text and "/responses" in text)):
        q = _AZURE_QUIRKS.setdefault(_quirk_key(c), set())
        q.discard("reasoning_none")
        q.add("use_responses")
        out.pop("reasoning_effort", None)
        return out
    if refuses_none:
        return None
    if "max_tokens" in text and "max_tokens" in out and (
            "max_completion_tokens" in text or "unsupported" in text):
        out["max_completion_tokens"] = out.pop("max_tokens")
        learned.add("rename_max_tokens")
    if "temperature" in text and "temperature" in out and (
            "unsupported" in text or "does not support" in text):
        out.pop("temperature")
        learned.add("drop_temperature")
    if "reasoning_effort" in text and out.get("reasoning_effort") != "none" and (
            "'none'" in text or '"none"' in text or " none" in text):
        out["reasoning_effort"] = "none"
        learned.add("reasoning_none")
    if not learned:
        return None
    if c is not None:
        _AZURE_QUIRKS.setdefault(_quirk_key(c), set()).update(learned)
    return out


def _explain_azure(status, body_text):
    """Azure's failures have different causes than OpenRouter's, and the useful
    remedy differs too -- a 404 here almost always means the deployment name in
    the URL is wrong, not that a model is unavailable."""
    detail = ""
    try:
        parsed = json.loads(body_text)
        err = parsed.get("error") or {}
        detail = err.get("message") or parsed.get("message") or ""
    except Exception:
        detail = (body_text or "")[:300]

    if status == 401:
        return LLMError("Azure rejected the API key. Check the key for this resource.",
                        status, "bad_key")
    if status == 403:
        return LLMError(detail or "Azure refused this request -- often a network or firewall "
                                  "rule on the resource, or a key without access to this "
                                  "deployment.", status, "forbidden")
    if status == 404 or (status == 400 and "deploymentnotfound" in (body_text or "").lower()):
        return LLMError("Azure could not find that deployment. Check the deployment name (in the "
                        "URL path on a deployments/... endpoint, or the model name on a v1 "
                        "endpoint) exists in this resource.",
                        status, "no_model")
    if status == 429:
        return LLMError("Azure rate limited this request -- the deployment's tokens-per-minute "
                        "quota. Wait, or raise the quota in the Azure portal.",
                        status, "rate_limited")
    if status and status >= 500:
        return LLMError("Azure OpenAI had a server error. Try again.", status, "upstream")
    if status == 400 and "reasoning_effort" in detail.lower():
        return LLMError("This Azure deployment will not do tool calling on chat completions "
                        "while reasoning. Use a v1 endpoint (https://<resource>.openai.azure.com/"
                        "openai/v1/chat/completions), which lets the app switch it to the "
                        "Responses API automatically. Azure said: " + detail,
                        status, "unsupported_model")
    if status == 400 and "content" in detail.lower() and "filter" in detail.lower():
        return LLMError("Azure's content filter blocked this request or response. Debug logs can "
                        "trip it; try rephrasing, or have the filter relaxed for this deployment.",
                        status, "content_filter")
    return LLMError(detail or f"Azure returned {status}.", status, "llm_error")


def _explain(status, body_text):
    """Turn a provider status into something a support engineer can act on.
    A bare '429' in the transcript helps nobody."""
    detail = ""
    try:
        parsed = json.loads(body_text)
        detail = (parsed.get("error") or {}).get("message") or parsed.get("message") or ""
    except Exception:
        detail = (body_text or "")[:300]

    if status == 401:
        return LLMError("Your OpenRouter key was rejected. Update it in LLM settings.",
                        status, "bad_key")
    if status == 402:
        return LLMError("Out of OpenRouter credits. Top up the account behind this key.",
                        status, "no_credits")
    if status == 403:
        return LLMError(detail or "OpenRouter refused this request for this key.", status, "forbidden")
    if status == 404:
        return LLMError("That model is not available on OpenRouter. Pick another one.",
                        status, "no_model")
    if status == 429:
        return LLMError("Rate limited by OpenRouter. Wait a moment and try again.", status, "rate_limited")
    if status and status >= 500:
        return LLMError("OpenRouter had a server error. Try again, or switch model.", status, "upstream")
    return LLMError(detail or f"OpenRouter returned {status}.", status, "llm_error")


# ---------- model catalogue ----------

async def list_models_for(c, force=False):
    """Azure has no catalogue to list. The deployment in the endpoint URL is the
    model, so the picker shows exactly that one entry -- which is honest: on
    Azure you change models by pointing at a different deployment, not by
    choosing from a menu."""
    if c["provider"] == PROVIDER_AZURE:
        name = azure_model(c["endpoint"], c.get("model"))
        return [{
            "id": name,
            "name": f"{name} (Azure deployment)",
            "context_length": None,
            "prompt_price": None, "completion_price": None,
            "is_free": False,
            "agentic_index": None, "intelligence_index": None,
            "supports_tool_choice": True,
            "expires": None,
            "note": "Azure OpenAI deployment. To use a different model, point the endpoint "
                    "at a different deployment in LLM settings.",
        }]
    return await list_models(c["api_key"], force=force)


async def list_models(api_key, force=False):
    """Tool-capable models only.

    This filter is not a nicety. Most of OpenRouter's free models cannot call
    tools at all, and a chat using one silently never touches the org
    knowledgebase -- it just makes things up fluently. Offering them would be
    the single most confusing thing this feature could do.
    """
    import time
    now = time.time()
    if not force and _MODEL_CACHE["models"] is not None and now - _MODEL_CACHE["at"] < MODEL_CACHE_TTL:
        return _MODEL_CACHE["models"]

    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=30.0)) as c:
        try:
            r = await c.get(f"{OPENROUTER_BASE}/models", headers=_headers(api_key))
        except httpx.RequestError as e:
            raise LLMError(f"Could not reach OpenRouter: {e.__class__.__name__}", None, "unreachable")
    if r.status_code >= 400:
        raise _explain(r.status_code, r.text)

    out = []
    for m in (r.json().get("data") or []):
        supported = m.get("supported_parameters") or []
        if "tools" not in supported:
            continue
        pricing = m.get("pricing") or {}
        # OpenRouter publishes Artificial Analysis scores, and `agentic_index`
        # is the one that predicts whether a model survives this workload --
        # multi-round tool calling over long JSON. It tracks the failures seen
        # in practice far better than parameter count or coding score, so it is
        # surfaced in the picker rather than left for the user to guess at.
        aa = ((m.get("benchmarks") or {}).get("artificial_analysis") or {})
        out.append({
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "context_length": m.get("context_length"),
            "prompt_price": _to_float(pricing.get("prompt")),
            "completion_price": _to_float(pricing.get("completion")),
            "is_free": _to_float(pricing.get("prompt")) == 0.0 and _to_float(pricing.get("completion")) == 0.0,
            "agentic_index": aa.get("agentic_index"),
            "intelligence_index": aa.get("intelligence_index"),
            # Not every model accepts tool_choice. Sending it to one that does
            # not is a needless compatibility risk, so record support here and
            # let stream_chat decide.
            "supports_tool_choice": "tool_choice" in supported,
            "expires": m.get("expiration_date"),
        })
    # Best agentic score first: that is the order that answers "which should I
    # pick". Unscored models sort after scored ones rather than to the top.
    out.sort(key=lambda m: (-(m["agentic_index"] if m["agentic_index"] is not None else -1),
                            m["name"].lower()))
    _MODEL_CACHE["models"] = out
    _MODEL_CACHE["at"] = now
    return out


def tool_choice_supported(model_id):
    """From the cached catalogue; True when unknown, since omitting tool_choice
    is the safer default only where we positively know it is unsupported."""
    for m in (_MODEL_CACHE.get("models") or []):
        if m["id"] == model_id:
            return m.get("supports_tool_choice", True)
    return True


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def verify_key(api_key):
    """Cheapest possible proof that a key works, used before we agree to store
    it. /models with a bad key 401s, so no tokens are spent."""
    await list_models(api_key, force=True)
    return True


async def verify_creds(c):
    """Prove a credential bundle works before storing it.

    OpenRouter can be checked for free against /models. Azure has no equivalent
    on a deployment-scoped URL, so this sends a one-token completion -- which
    also proves the deployment name (path or v1 model) and api-version are
    right, something a catalogue lookup could never tell us. The cost is a
    handful of tokens.
    """
    if c["provider"] != PROVIDER_AZURE:
        await verify_key(c["api_key"])
        return True

    c = dict(c, endpoint=validate_azure_endpoint(c["endpoint"]))
    c["model"] = validate_azure_model(c["endpoint"], c.get("model"))
    # The ping carries a tool definition and a temperature on purpose: every
    # real chat round sends both, and some deployments accept a plain prompt
    # but refuse tools (e.g. "Function tools with reasoning_effort are not
    # supported"). Testing without them let such a connection save cleanly
    # and then fail on the first question.
    url, headers, body = _prepare(c, {
        "messages": [{"role": "user", "content": "Reply with the word ok."}],
        "max_tokens": 16, "temperature": 0.2, "stream": False,
        "tools": [{"type": "function", "function": {
            "name": "noop", "description": "Unused connectivity check.",
            "parameters": {"type": "object", "properties": {}}}}],
        "tool_choice": "auto",
    })
    body = _apply_quirks(c, body)
    async with httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, read=60.0)) as client:
        try:
            r = None
            for _ in range(MAX_AZURE_ATTEMPTS):
                if uses_responses_api(c):
                    # A reasoning model can spend a tiny budget thinking and come
                    # back "incomplete"; that still proves the key, deployment
                    # and tool definition were all accepted.
                    rbody = _to_responses_request(dict(body, max_tokens=256), stream=False)
                    r = await client.post(azure_responses_url(c["endpoint"]),
                                          headers=headers, json=rbody)
                    break
                r = await client.post(url, headers=headers, json=body)
                retry = _azure_retry_body(body, r.status_code, r.text, c)
                if retry is None:
                    break
                body = retry
        except httpx.RequestError as e:
            raise LLMError(f"Could not reach that Azure endpoint ({e.__class__.__name__}). "
                           f"Check the hostname, and that this server can reach it.",
                           None, "unreachable")
    if r.status_code >= 400:
        raise _explain_azure(r.status_code, r.text)
    return True


# ---------- Azure Responses API ----------
#
# Some Azure deployments (gpt-6-luna is the one that forced this) reason by
# default, refuse function tools on /chat/completions while reasoning, AND
# refuse reasoning_effort="none". The only way to give them tools is the
# Responses API (/openai/v1/responses). Rather than fork the agent loop, this
# section translates at the edge: chat-completions messages in, the same
# Delta stream out, so chat.py cannot tell which API answered.
#
# Deliberately not done: carrying encrypted reasoning items between rounds
# (`include: ["reasoning.encrypted_content"]`). Without them the model
# re-reasons after each tool result instead of resuming -- slower, but
# correct, and nothing model-internal lands in our transcripts.

def uses_responses_api(c):
    return "use_responses" in (_AZURE_QUIRKS.get(_quirk_key(c)) or set())


def azure_responses_url(endpoint):
    """The Responses route beside a v1 chat-completions URL (query kept)."""
    parsed = urlparse(endpoint)
    path = re.sub(r"/chat/completions$", "/responses", parsed.path.rstrip("/"))
    return parsed._replace(path=path).geturl()


def _to_responses_request(chat_body, stream):
    """Translate a chat-completions body into a Responses body.

    `temperature` is never sent: only reasoning-class deployments are routed
    here, and they accept only the default. `strict: False` on every tool is
    load-bearing -- the Responses API defaults function tools to strict mode,
    which would reject the app's MCP schemas (optional properties, no
    additionalProperties: false).
    """
    items = []
    for m in chat_body.get("messages") or []:
        role = m.get("role")
        if role == "system":
            items.append({"role": "developer", "content": m.get("content") or ""})
        elif role == "user":
            items.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({"type": "function_call", "call_id": tc.get("id"),
                              "name": fn.get("name"), "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                          "output": m.get("content") or ""})

    body = {"model": chat_body.get("model"), "input": items, "store": False, "stream": stream}
    tools = []
    for t in chat_body.get("tools") or []:
        fn = t.get("function") or {}
        tools.append({"type": "function", "name": fn.get("name"),
                      "description": fn.get("description") or "",
                      "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                      "strict": False})
    if tools:
        body["tools"] = tools
        body["tool_choice"] = chat_body.get("tool_choice") or "auto"
    limit = chat_body.get("max_tokens") or chat_body.get("max_completion_tokens")
    if limit:
        body["max_output_tokens"] = max(int(limit), 16)    # the API's floor is 16
    return body


def _responses_usage(u):
    """Responses usage, in the chat-completions shape chat._usage reads."""
    u = u or {}
    return {
        "prompt_tokens": u.get("input_tokens"),
        "completion_tokens": u.get("output_tokens"),
        "total_tokens": u.get("total_tokens"),
        "completion_tokens_details": {
            "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens")},
    }


class _ResponsesStream:
    """Turns Responses SSE events into Deltas. Tool calls are re-indexed in
    arrival order, because accumulate_tool_calls correlates fragments by
    `index` exactly as chat-completions streams them."""

    def __init__(self):
        self.index_of = {}          # item id -> tool-call index
        self.got_args = set()       # item ids whose arguments have arrived

    def feed(self, ev):
        t = ev.get("type") or ""
        if t == "response.output_text.delta":
            return [Delta(content=ev.get("delta"))] if ev.get("delta") else []
        if t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            return [Delta(reasoning=ev.get("delta"))] if ev.get("delta") else []
        if t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") != "function_call":
                return []
            iid = item.get("id") or f"out{ev.get('output_index')}"
            idx = len(self.index_of)
            self.index_of[iid] = idx
            args = item.get("arguments") or ""
            if args:
                self.got_args.add(iid)
            return [Delta(tool_calls=[{"index": idx, "id": item.get("call_id"),
                                       "function": {"name": item.get("name"),
                                                    "arguments": args}}])]
        if t == "response.function_call_arguments.delta":
            idx = self.index_of.get(ev.get("item_id"))
            if idx is None or not ev.get("delta"):
                return []
            self.got_args.add(ev.get("item_id"))
            return [Delta(tool_calls=[{"index": idx, "function": {"arguments": ev["delta"]}}])]
        if t == "response.output_item.done":
            # Some gateways deliver a call's arguments only in the final item.
            item = ev.get("item") or {}
            iid = item.get("id")
            if (item.get("type") == "function_call" and iid in self.index_of
                    and iid not in self.got_args and item.get("arguments")):
                self.got_args.add(iid)
                return [Delta(tool_calls=[{"index": self.index_of[iid],
                                           "function": {"arguments": item["arguments"]}}])]
            return []
        if t in ("response.completed", "response.incomplete"):
            resp = ev.get("response") or {}
            if t == "response.incomplete":
                reason = (resp.get("incomplete_details") or {}).get("reason") or "length"
                finish = "length" if reason == "max_output_tokens" else reason
            else:
                finish = "tool_calls" if self.index_of else "stop"
            return [Delta(finish_reason=finish, usage=_responses_usage(resp.get("usage")))]
        if t in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
            raise LLMError("Azure OpenAI failed this response: "
                           f"{err.get('message') or err.get('code') or 'unknown error'}",
                           None, "upstream")
        return []


async def _stream_responses(client, c, chat_body, headers, explain):
    body = _to_responses_request(chat_body, stream=True)
    async with client.stream("POST", azure_responses_url(c["endpoint"]),
                             headers=headers, json=body) as response:
        if response.status_code >= 400:
            text = (await response.aread()).decode("utf-8", errors="replace")
            raise explain(response.status_code, text)
        parser = _ResponsesStream()
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue                     # `event:` lines repeat the type in the data
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            for delta in parser.feed(ev):
                yield delta


# ---------- streaming completions ----------

class Delta:
    """One increment of the response: text, reasoning text, tool-call
    fragments, a finish reason, or the final usage block."""

    __slots__ = ("content", "reasoning", "tool_calls", "finish_reason", "usage")

    def __init__(self, content=None, reasoning=None, tool_calls=None, finish_reason=None, usage=None):
        self.content = content
        self.reasoning = reasoning
        self.tool_calls = tool_calls
        self.finish_reason = finish_reason
        self.usage = usage


async def stream_chat(c, model, messages, tools=None,
                      temperature=0.2, max_tokens=None) -> AsyncIterator[Delta]:
    """Yield Delta objects as the model produces them.

    `usage: {include: true}` asks OpenRouter to append a usage block to the
    final chunk -- token counts, reasoning tokens, and the actual cost of the
    call, which is what the UI shows under each turn.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "usage": {"include": True},
    }
    if tools:
        payload["tools"] = tools
        # Azure always accepts tool_choice; on OpenRouter it is per-model.
        if c["provider"] == PROVIDER_AZURE or tool_choice_supported(model):
            payload["tool_choice"] = "auto"
    if max_tokens:
        payload["max_tokens"] = max_tokens

    url, headers, payload = _prepare(c, payload)
    is_azure = c["provider"] == PROVIDER_AZURE
    if is_azure:
        payload = _apply_quirks(c, payload)
    explain = _explain_azure if is_azure else _explain

    timeout = httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT, write=30.0, pool=CONNECT_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            # Retries happen only on Azure, and only when a 400 names a
            # parameter the deployment's model family does not accept (see
            # _azure_retry_body). Each retry fixes a different parameter, and
            # the fix is remembered, so this costs at most a few rejected
            # requests per deployment per process. Nothing has been yielded
            # at that point, so the retry is invisible to the caller.
            for attempt in range(MAX_AZURE_ATTEMPTS if is_azure else 1):
                if is_azure and uses_responses_api(c):
                    async for delta in _stream_responses(client, c, payload, headers, explain):
                        yield delta
                    return
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        retry = (_azure_retry_body(payload, response.status_code, body, c)
                                 if is_azure and attempt < MAX_AZURE_ATTEMPTS - 1 else None)
                        if retry is not None:
                            payload = retry
                            continue
                        raise explain(response.status_code, body)

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue      # OpenRouter sends ": OPENROUTER PROCESSING" keepalives
                        for delta in _deltas_from_chunk(chunk):
                            yield delta
                    return
        except httpx.RequestError as e:
            who = "Azure OpenAI" if c["provider"] == PROVIDER_AZURE else "OpenRouter"
            raise LLMError(f"Lost the connection to {who} ({e.__class__.__name__}).",
                           None, "unreachable")


def _deltas_from_chunk(chunk):
    out = []
    usage = chunk.get("usage")
    choices = chunk.get("choices") or []
    if not choices:
        if usage:
            out.append(Delta(usage=usage))
        return out

    choice = choices[0]
    d = choice.get("delta") or {}
    content = d.get("content")
    reasoning = d.get("reasoning")
    tool_calls = d.get("tool_calls")
    finish = choice.get("finish_reason")

    if content or reasoning or tool_calls or finish or usage:
        out.append(Delta(content=content, reasoning=reasoning, tool_calls=tool_calls,
                         finish_reason=finish, usage=usage))
    return out


def accumulate_tool_calls(acc: dict, fragments):
    """Merge streamed tool-call fragments into whole calls.

    The provider streams a tool call in pieces: an opening chunk with the id
    and name, then a run of chunks each carrying a slice of the JSON argument
    string. Fragments are correlated by `index`, NOT by id -- the id only
    appears on the first fragment. Getting this wrong produces tool calls with
    truncated arguments, which fail in confusing ways much later.
    """
    for frag in fragments or []:
        idx = frag.get("index", 0)
        slot = acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if frag.get("id"):
            slot["id"] = frag["id"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


# --- text-format tool calls -------------------------------------------------
#
# Some models -- overwhelmingly the free and heavily quantized ones -- were
# fine-tuned to emit tool calls as markup inside the message body instead of in
# the native `tool_calls` field, and OpenRouter passes that through verbatim.
# The same model often does it inconsistently: native on one round, markup on
# the next. Left unhandled the user is shown raw `<tool_call>` XML as if it were
# an answer, which is what this parser exists to prevent.
#
# Two dialects are common, and both appear inside <tool_call> ... </tool_call>:
#
#   A)  <function=tool_name>
#         <parameter=arg_name>value</parameter>
#       </function>
#
#   B)  {"name": "tool_name", "arguments": {"arg_name": "value"}}

_TOOL_BLOCK_RE = re.compile(
    r"<(tool_call|function_calls)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_FUNCTION_RE = re.compile(
    r"<function\s*=\s*([\w.-]+)\s*>(.*?)</function\s*>", re.DOTALL | re.IGNORECASE)
_PARAM_RE = re.compile(
    r"<parameter\s*=\s*([\w.-]+)\s*>(.*?)</parameter\s*>", re.DOTALL | re.IGNORECASE)


def _coerce(value: str):
    """Markup carries no types -- everything arrives as a string. Recover the
    obvious ones so a boolean parameter does not reach a tool as "true"."""
    v = value.strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", v):
        try:
            return int(v)
        except ValueError:
            return v
    if re.fullmatch(r"-?\d*\.\d+", v):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def extract_text_tool_calls(text: str):
    """Pull tool calls out of message text.

    Returns (calls, cleaned_text). `calls` matches the shape finalize_tool_calls
    produces, so the agent loop can treat them identically. `cleaned_text` is the
    message with the markup removed, so the user never sees it.
    """
    if not text or "<tool_call" not in text.lower() and "<function_calls" not in text.lower():
        return [], text

    calls, index = [], 0
    for block in _TOOL_BLOCK_RE.finditer(text):
        body = block.group(2).strip()

        matched = False
        for fn in _FUNCTION_RE.finditer(body):
            name = fn.group(1)
            args = {k: _coerce(v) for k, v in _PARAM_RE.findall(fn.group(2))}
            calls.append({"id": f"text_call_{index}", "name": name, "args": args,
                          "raw_arguments": json.dumps(args), "parse_error": None,
                          "from_text": True})
            index += 1
            matched = True

        if matched:
            continue

        # dialect B: a JSON object naming the tool
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("name"):
            args = parsed.get("arguments") or parsed.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": f"text_call_{index}", "name": parsed["name"],
                          "args": args if isinstance(args, dict) else {},
                          "raw_arguments": json.dumps(args), "parse_error": None,
                          "from_text": True})
            index += 1

    cleaned = _TOOL_BLOCK_RE.sub("", text).strip()
    return calls, cleaned


def finalize_tool_calls(acc: dict):
    """Ordered list of {id, name, args} with arguments parsed.

    A model can emit malformed JSON arguments. We surface that as a parse
    error the loop feeds back to the model rather than raising, because the
    model can usually correct itself on the next round.
    """
    calls = []
    for idx in sorted(acc.keys()):
        slot = acc[idx]
        if not slot.get("name"):
            continue
        raw = slot.get("arguments") or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
            parse_error = None
        except json.JSONDecodeError as e:
            args, parse_error = {}, f"Could not parse arguments as JSON: {e}"
        calls.append({
            "id": slot.get("id") or f"call_{idx}",
            "name": slot["name"],
            "args": args,
            "raw_arguments": raw,
            "parse_error": parse_error,
        })
    return calls
