---
name: aapanel-deploy-site
description: Use when putting a website onto an aaPanel server with the aapanel MCP tools - a new domain, a Laravel or other PHP app, a WordPress install, a static site, or a reverse proxy in front of a Node or Python service, together with its database and HTTPS.
---

# Deploying a site on aaPanel

## Overview

Two things about this panel break deployments quietly, with no error to read:

- **Writing config is not applying config.** `site_rewrite_set` saves a file. The web server keeps serving the old one until it reloads.
- **The default document root is the project root.** For any framework that serves from a subdirectory, that publishes the whole repository.

Neither failure raises anything. The site returns 200 and is wrong.

## Before the first write call

- `site_list` — confirm the domain is not already bound to another site.
- `site_php_versions` — REQUIRED for a PHP site. Copy the id out of the response and pass it through unchanged. The panel wants `"82"`; `"8.2"` is not a version it knows.
- Ask the user, and wait: where the code comes from, whether DNS for the domain already resolves to this server, and which email to register with Let's Encrypt.

## The sequence

1. **`site_create`** — `domain`, `php_version` (from the check above, or `"00"` for a static or proxy-only site), `create_database=true` when the app needs MySQL.

   Relay `database_password` and `ftp_password` from the response in your next message. The panel returns them once and has no way to show them again.

2. **`site_set_run_path`** — REQUIRED for Laravel, Symfony, and anything else with a `public/` front controller. Pass `run_path="/public"`.

   Skipping this leaves the document root at the project root, where `.env`, `composer.json`, `storage/` and `.git` are all fetchable over HTTP. The app still serves normally, so nothing surfaces the mistake.

3. **`site_rewrite_templates`** then **`site_rewrite_set`** — take the template the panel ships for the framework instead of writing rules by hand.

4. **`service_restart`** — REQUIRED after `site_rewrite_set`, and after that tool alone. Pass `service="webserver"`, `mode="reload"`.

   `site_rewrite_set` saves the config file directly and never reloads. Every other tool in this sequence goes through a panel endpoint that reloads for you, so reloading again after them buys nothing. Until this call the file on disk is right and the running server is not: every URL except the front page returns 404, with nothing in the logs to explain it.

5. **`site_create_proxy`** — for an upstream service. Set `proxy_dir` to the subpath it owns, such as `/api`; a proxy on a subpath leaves `/.well-known/` alone and needs nothing special in step 6.

   Reserve `proxy_dir="/"` for a site that serves nothing else. A root proxy answers `/.well-known/` as well, so the certificate for that site has to come from `dns_api`. Do not reach for the reordering instead: issuing over HTTP-01 first and adding the root proxy afterwards does produce a working certificate, and then the panel renews it the same way it issued it, the proxy answers the challenge, and the certificate lapses on day ninety with nothing raised.

6. **`ssl_issue_letsencrypt`** — the default HTTP-01 validation needs the domain resolving to this server and port 80 reachable. Pass `dns_api` instead for a wildcard, or when a proxy at `/` is intercepting `/.well-known/`.

7. **`ssl_set_force_https`** — only after `ssl_site_status` shows a certificate installed. Enabling the redirect first sends every visitor to a port with no certificate.

8. Verify with `site_info`, `ssl_site_status`, and `site_logs`.

## Variations

| Site | Sequence |
|---|---|
| Laravel, Symfony | all eight steps |
| WordPress, plain PHP | skip 2 |
| Static | 1 with `php_version="00"`, then 6-8 |
| Node or Python upstream | 1 with `php_version="00"`, then 5-8 |

## Common mistakes

| Mistake | What happens |
|---|---|
| `site_rewrite_set` with no reload after it | Every route but `/` returns 404. Config on disk looks correct. |
| No `site_set_run_path` on a Laravel site | `https://site/.env` serves the database password. |
| `php_version="8.2"` | The panel does not recognise the id. Use the string from `site_php_versions`. |
| `ssl_set_force_https` before the certificate | Site unreachable over both schemes. |
| `proxy_dir="/"` with an HTTP-01 certificate | Issuance fails, or renewal does. A root proxy needs `dns_api`. |
| Password left in the tool result | `site_create` returns it once. Not relayed means lost. |
