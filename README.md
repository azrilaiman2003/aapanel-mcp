# aaPanel MCP Server

A third-party aaPanel plugin that turns the panel into an MCP server, so an AI agent can
manage the machine: websites, TLS certificates, databases, mail, FTP, cron, files, the
firewall, Docker, services and installed apps.

It installs like any other third-party plugin — build a zip, import it in the app store —
and ships closed: loopback only, read-only tools, everything else off until you turn it on.

---

## Install

```bash
./build.sh                      # writes dist/aapanel_mcp-1.0.0.zip
```

In aaPanel: **App Store → Third-party → Import**, pick the zip, then open **AI MCP Server**.

On the Overview tab:

1. **Enable local panel API** — the plugin needs the panel's own API switched on for
   127.0.0.1. It generates a key if there isn't one and never asks you to copy it.
2. **Start** the server if it is not already running.
3. Copy one of the connection snippets.

```bash
claude mcp add --transport http aapanel https://your-server:7801/mcp \
  --header "Authorization: Bearer <token>"
```

Then widen the permissions on the **Permissions** tab — a fresh install can only read.

## What the agent gets

104 tools across thirteen domains. Everything the panel exposes over HTTP is reachable;
the tools cover the common paths with validated arguments, and two raw tools cover the
rest.

| Domain | Tools |
|---|---|
| `sites` | list, inspect, create, delete, domains, PHP version, run directory, rewrite rules, reverse proxies, logs, expiry, start/stop |
| `ssl` | certificate list, per-site status, Let's Encrypt issuance (HTTP-01 or DNS-01), upload, force HTTPS, renew, disable |
| `databases` | list, inspect, create, delete, password, access grants, backup, SQL import, server status, slow log |
| `mail` | capability discovery, then read / manage / delete against the installed mail plugin |
| `ftp` | list, create, delete, password, enable/disable |
| `files` | list, read, write, search, copy, move, delete, permissions, compress, extract, size |
| `cron` | list, create, delete, run now, enable/disable, logs |
| `firewall` | rules, open/close port, block/unblock address, SSH settings and port |
| `system` | load and disk, versions, processes, installed software, panel log, background tasks, service control, reboot |
| `docker` | containers, images, networks, volumes, logs, control, delete, system info, raw module call |
| `apps` | installed apps, app store, status, install, uninstall |
| `shell` | run a command as root, read the last command's output |
| `raw` | any panel endpoint, any plugin method, any data table |

Mail and Docker tools only appear when the corresponding app is installed.

### Mail, and why it works differently

aaPanel has no `/mail` route — the mail server is a plugin, and its method names change
between versions with nothing published to pin them to. Rather than shipping guesses,
`mail_capabilities` reads the installed plugin's own source and reports every callable
method with the parameters it takes. The agent calls that first, then `mail_read`,
`mail_manage` or `mail_delete` depending on what it needs. The three-way split is what
keeps the permission tiers meaningful for a surface nobody documented.

## Permissions

Every tool sits in exactly one tier:

| Tier | What it covers | Default |
|---|---|---|
| `read` | Listing and inspecting | **on** |
| `write` | Creating and editing | off |
| `destructive` | Deleting, stopping services, rebooting | off |
| `shell` | Arbitrary commands as root | off |
| `raw` | Direct panel and plugin calls | off |

A disabled tool is **absent from `tools/list`**, not merely refused — an agent cannot ask
for a capability it never saw. Individual tools can be forced on or off against their tier.

Tools in the destructive and shell tiers refuse their first call and return a summary plus
a confirmation token; the agent has to show you the summary and repeat the call with the
token. The token is an HMAC over the tool name and the exact arguments, so it does not
transfer to a different call, and it expires after five minutes.

Every call — including refused ones — is written to `data/audit.log`, readable on the
Audit tab. Passwords, keys and tokens are stripped before anything is written.

## Exposure

Three modes, on the Access tab:

- **This server only** (default) — binds 127.0.0.1. Reach it through an SSH tunnel.
- **A network port** — binds an address, optionally with TLS from a self-signed
  certificate, the panel's own, or a file you point it at. Open the port from the same tab.
- **A proxied domain** — the plugin creates an aaPanel site that reverse-proxies to the
  loopback endpoint, so you can issue a real certificate for it under Website → SSL.

In every mode: bearer token, optional IP allowlist, `Origin` rejection (MCP clients send
no `Origin`, so the default of "reject anything that does" blocks DNS rebinding without
blocking real clients), a per-address rate limit and a body size cap.

There is also a stdio entry point, which needs no open port at all:

```bash
claude mcp add aapanel -- ssh root@server \
  /www/server/panel/plugin/aapanel_mcp/bin/aapanel-mcp-stdio
```

## Use with Claude Code

This repository is also a Claude Code plugin. Installing it registers the MCP server and
adds skills for the jobs that span several tools and that no single tool description can
describe on its own.

```bash
/plugin marketplace add azrilaiman2003/aapanel-mcp
/plugin install aapanel-mcp@aapanel-mcp
```

The server entry reads two environment variables, which must be set before Claude Code
starts or the connection fails:

```bash
export AAPANEL_MCP_URL=https://your-server:7801/mcp
export AAPANEL_MCP_TOKEN=<the token from the Access tab>
```

Prefer no open port? Skip the plugin's server entry and add the stdio one by hand, as
under Exposure above; the skills work against either.

| Skill | Covers |
|---|---|
| `aapanel-deploy-site` | Putting a site on the server: document root, rewrite rules and the reload they need, reverse proxies, database, certificate, forced HTTPS |
| `aapanel-release-app` | Shipping a new version to a site that exists: batching shell calls against the confirmation cost, exit status, waiting for the backup, migrations, file ownership after a root build |

## Protocol

Dual-era, on one endpoint. It speaks **2026-07-28** — stateless, per-request `_meta`,
mandatory `server/discover`, `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` header
validation with `-32020` on mismatch, `resultType` on every result, `ttlMs`/`cacheScope`
cache hints on listing results — and also answers the older `initialize` handshake used by
`2025-11-25` and earlier, including the GET SSE channel those clients expect. Whatever MCP
client you already have will connect.

`subscriptions/listen` is supported for `toolsListChanged`, so a client sees the tool list
change when you adjust permissions without reconnecting.

## How it is built

```
plugin/aapanel_mcp/
├── aapanel_mcp_main.py     panel-side control surface (start/stop, config, audit)
├── aapanel_mcp_service     the HTTP daemon
├── bin/aapanel-mcp-stdio   the stdio entry point
├── index.html              the plugin UI
└── mcpd/
    ├── panel_client.py     signed calls to the local panel API
    ├── protocol.py         MCP dispatch, both eras
    ├── http_transport.py   Streamable HTTP, TLS, gatekeeping
    ├── stdio_transport.py
    ├── registry.py         tool definitions and schema validation
    ├── permissions.py      tiers and confirmation tokens
    ├── audit.py
    └── tools/              one module per domain
```

Two decisions worth knowing:

**No third-party dependencies.** Standard library only. aaPanel servers are often
firewalled off from PyPI, and pip-installing into the panel's virtualenv is a good way to
break the panel. That covers the MCP protocol, the HTTP server, TLS and JSON Schema
validation.

**The daemon talks to the panel over HTTP, not by importing it.** Panel modules do
`from BTPanel import session, cache` and call `public.GetClientIp()`, which need a live
Flask request context; importing them from an outside process works until it doesn't, and
breaks differently on every release. Instead the daemon reads `config/api.json` — where
the panel already stores `md5(api_key)` — and signs each call the way
`class/common.py: get_sk` verifies it: `request_token = md5(request_time + token)`. Being
root on the same box is what makes this work without anyone copying a secret.

The plugin process and the daemon are separate: restarting the panel does not interrupt an
agent mid-task, and a daemon crash cannot take the panel down.

## Development

```bash
python3 -m unittest discover -s tests -t tests   # 118 tests, no aaPanel needed
python3 tests/live_check.py                      # runs the real daemon against a fake panel
./build.sh                                       # dist/aapanel_mcp-<version>.zip
python3 scripts/make_icon.py                     # redraw icon.png
```

The suite runs against a fake aaPanel that enforces the real token rule, so the client is
tested against the same contract the live panel applies. `scripts/mcp_probe.py` also works
against a real install:

```bash
python3 scripts/mcp_probe.py http://127.0.0.1:7801/mcp --token TOKEN --call site_list
```

If a plugin is installed but misbehaving, `bash repair.sh` inside the plugin directory
checks the files, clears stale bytecode, runs a self-check and prints the last log lines.

## Security

This plugin hands an AI agent the keys to a hosting server. Worth being deliberate about:

- The bearer token is equivalent to a panel login at whatever tier level you enabled.
  Rotating it disconnects existing clients, which is the point.
- Enabling `shell` or `raw` means an agent holding the token can do anything root can.
  The Overview tab states the current posture in one sentence so it is never a surprise.
- Prefer the loopback or proxied modes over an open port. If you do bind a port, use TLS —
  without it the token crosses the network in clear text.
- The audit log is the record of what actually happened. Read it after any session where
  the agent had write access.

## Licence

MIT.
