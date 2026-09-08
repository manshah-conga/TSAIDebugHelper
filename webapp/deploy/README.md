# Deploying the remote MCP endpoint

The web app now serves its MCP tools at `POST /mcp` over Streamable HTTP.
A client needs two things and nothing else:

    URL:    https://44.226.216.180/mcp
    Header: Authorization: Bearer <API token from the web UI>

No Python on the client, no copy of `mcp_server.py`, no per-machine config
file. The local stdio path still works unchanged for anyone already set up
that way.

---

## 1. What changed in the code

| File | Change |
|---|---|
| `mcp_server.py` | Token now resolves per request from a `ContextVar`, falling back to `TS_DEBUG_HELPER_TOKEN` for stdio. `stateless_http=True`. Tool definitions untouched. |
| `app/mcp_http.py` | **New.** Streamable HTTP transport, token extraction from the request, edge validation. |
| `app/main.py` | `on_event("startup")` became a `lifespan` (it now also runs the MCP session manager); `MCPTransportMiddleware` added. |
| `requirements.txt` | `mcp` pinned to `>=1.9,<2`. |

That last pin matters: `mcp` 2.x renamed `FastMCP` to `MCPServer`, so the
previous unpinned `mcp>=1.0` would have broken a fresh install on its own.

### Why middleware rather than `app.mount("/mcp", ...)`

A Starlette mount answers a request for the bare `/mcp` with a 307 redirect
to `/mcp/`. MCP clients POST JSON-RPC and do not reliably re-issue the body
against the redirect target, so the connection just fails. Intercepting above
the router avoids that and accepts `/mcp` and `/mcp/` alike.

### Why `stateless_http=True`

Every request carries its own token and is executed as that token's owner.
Nothing is held server-side between calls, so the endpoint survives a
restart, works behind a proxy that does not pin a client to one worker, and
cannot leak one caller's identity into another's call.

---

## 2. Token on the request, not in the environment

Accepted, in priority order:

1. `Authorization: Bearer <token>` — preferred.
2. `X-TS-Token: <token>` — for clients that reserve `Authorization`.
3. `?token=<token>` — last resort, for clients that cannot set headers.

The third one puts a credential in the query string, where proxy and access
logs will capture it. Treat a token used that way as lower-trust: give it the
`reader` role and a short TTL. Turn it off entirely with
`TS_MCP_ALLOW_QUERY_TOKEN=0` once no client needs it.

The token is verified at the transport edge so a bad one fails the connection
with a clean 401 instead of turning into twenty tools that each return an
error string. The API routes verify it again — the edge check is for the
error message, not for enforcement.

**One endpoint, many people, each as themselves.** Every request runs under
its own token's role and org visibility, so a `reader` token still cannot
connect an org, and a private org stays invisible to everyone but its owner.
This is the part the environment-variable design could not do.

---

## 3. HTTPS on a bare IP address — no domain required

This is now genuinely possible with a publicly trusted certificate. As of
**15 January 2026** Let's Encrypt issues certificates for IP addresses
(IPv4 and IPv6). Two constraints come with them:

- They must use the **`shortlived` profile**: 160 hours, just under 7 days.
  Renewal has to be automated, and a broken renewal becomes an outage in days
  rather than weeks.
- Validation is **HTTP-01 or TLS-ALPN-01 only** — there is no DNS record to
  point at, so port 80 or 443 must be reachable from the public internet at
  renewal time.

### Issue the certificate

Needs certbot **5.4+** (`--ip-address` arrived in 5.3, webroot support for it
in 5.4). Check with `certbot --version`; install from snap if the distro
package is older.

```bash
# Dry run against staging first -- the production rate limit is unforgiving.
sudo certbot certonly --staging \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/html \
  --ip-address 44.226.216.180

# Then for real:
sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot --webroot-path /var/www/html \
  --ip-address 44.226.216.180
```

Because the lifetime is 160 hours, renew far more often than the usual
twice-daily default is designed for — twice a day is still fine, but confirm
the timer is actually running:

```bash
systemctl list-timers | grep certbot
sudo certbot renew --dry-run
```

### About the other VM (`https://3.238.81.16`)

An IP can serve HTTPS one of two ways, and they behave very differently:

- **A publicly trusted IP certificate** (the Let's Encrypt route above).
  Every client accepts it silently.
- **A self-signed certificate.** A browser shows a warning you can click
  through — which is why it "works" when you visit it — but Copilot Studio
  and Claude will both refuse the connection outright, with no
  click-through. Some setups paper over this with
  `NODE_TLS_REJECT_UNAUTHORIZED=0`, which disables certificate verification
  on the client and should not be copied here.

Check which one it is before assuming you can do the same:

```bash
openssl s_client -connect 3.238.81.16:443 </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

If `issuer` shows Let's Encrypt, that VM took the route above. If issuer and
subject are the same string, it is self-signed and only works because a human
clicked through.

---

## 4. Network reachability

This is the part that is not a code problem. Copilot Studio calls from
Microsoft's cloud and claude.ai calls from Anthropic's — neither is on your
VPN. For them to reach the server:

- **443 open to the public internet** (and **80**, for ACME validation).
- Port **8000 should be closed** to everything but loopback once nginx is in
  front. The systemd unit binds the app to `127.0.0.1` for exactly this reason.
- Security-group rules on the EC2 instance have to match.

If opening 443 publicly is not acceptable, the alternatives are a Power
Platform **on-premises data gateway** (Copilot Studio only), or an outbound
tunnel such as Cloudflare Tunnel, which gives you a public HTTPS hostname
without an inbound rule. Both are more setup than opening 443 on an app that
already requires a token for every request.

---

## 5. Install

```bash
sudo cp deploy/ts-debug-helper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ts-debug-helper

sudo cp deploy/nginx-ts-debug-helper.conf /etc/nginx/sites-available/ts-debug-helper
sudo ln -s /etc/nginx/sites-available/ts-debug-helper /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Verify:

```bash
# 401 with a clear reason, not a hang or a redirect:
curl -i -X POST https://44.226.216.180/mcp -d '{}'

# Full handshake:
curl -i -X POST https://44.226.216.180/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"curl","version":"1"}}}'
```

---

## 6. Client configuration

### Claude Code

```bash
claude mcp add --transport http ts-debug-helper https://44.226.216.180/mcp \
  --header "Authorization: Bearer <token>"
```

### Claude Desktop / claude.ai

Settings → Connectors → Add custom connector → `https://44.226.216.180/mcp`.

The connector UI is built around OAuth and may not expose a static header
field. If it does not, use the query-string form — with a `reader` token and
a short TTL — until OAuth is wired up:

```
https://44.226.216.180/mcp?token=<reader token>
```

### Copilot Studio

Agent → Tools → Add tool → New tool → Model Context Protocol.
URL `https://44.226.216.180/mcp`, auth **API Key**, parameter label
`Authorization`, location **Header**, value `Bearer <token>`.

Copilot Studio generates a Power Platform custom connector behind the scenes
carrying `x-ms-agentic-protocol: mcp-streamable-1.0`. All twenty tools appear
automatically. To surface it in Microsoft 365 Copilot, publish the agent to
the Microsoft 365 Copilot channel — M365 Copilot does not consume MCP
directly.

One consequence worth planning for: a Copilot Studio connector holds **one**
token, so every Copilot user shares that identity and role. Issue it a
dedicated `reader` token, or move the connector to OAuth 2.0 against Entra ID
and map identity in `app/auth.py`.

### Local stdio (unchanged)

```json
{
  "mcpServers": {
    "ts-debug-helper": {
      "command": "python",
      "args": ["C:\\Users\\manshah\\Claude\\Projects\\TS Intelligent Debug Helper\\webapp\\mcp_server.py"],
      "env": {
        "TS_DEBUG_HELPER_URL": "https://44.226.216.180",
        "TS_DEBUG_HELPER_TOKEN": "<token>"
      }
    }
  }
}
```

---

## 7. What was verified

Against a live server, before deployment:

- MCP `initialize` + `tools/list` over Streamable HTTP → 20 tools.
- Tool calls through all three token transports (Bearer, `X-TS-Token`, query).
- Missing token → 401 `missing_token`; invalid token → 401 `invalid_token`.
- Six concurrent sessions, no token bleeding between contexts.
- stdio entry point still works with the env-var token.
- Web UI and REST API unaffected.

Not verified here, because it needs the VM: TLS termination, the IP
certificate, and reachability from Microsoft's and Anthropic's clouds.
