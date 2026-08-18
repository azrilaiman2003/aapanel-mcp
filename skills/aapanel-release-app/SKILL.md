---
name: aapanel-release-app
description: Use when shipping a new version of code to a site that already exists on an aaPanel server through the aapanel MCP tools - pulling from git, composer or npm installs, building assets, running database migrations, rebuilding caches, restarting queue workers.
---

# Releasing a new version to an aaPanel site

## Overview

Three costs here, none of them visible in the tool descriptions:

- **Every `run_shell` call is its own confirmation round-trip.** The token is an HMAC over the tool name and the exact arguments, so no two commands can share one. Thirteen tidy commands is thirteen stop-and-ask cycles.
- **`run_shell` returns no exit status.** A failed `composer install` comes back `finished: true` with the error text in `output` — the same shape as success.
- **`database_backup` returns when the backup starts.** "Backup of shop started." is not "the backup exists."

## Two rules for every shell call

**Batch with `&&`.** Chain into as few calls as the checkpoints allow. A normal Laravel release is four: inspect, build, migrate, finish. Splitting them for tidiness costs the user a confirmation each time and buys nothing.

**End every command with `; echo "EXIT:$?"`** and read that line before calling the step successful. With `&&` chaining the status is the first command that failed, so one marker covers the whole batch. Without it you are guessing from prose.

**Timeouts:** the default 60 seconds is too short for dependency and asset work. Use 300 for `composer install` and `npm ci`, up to 600 for a large `npm run build`. `finished: false` means it is still running, not that it failed — poll `shell_last_output` and never re-issue the command.

## The sequence

1. **Read first.** `site_info` for the document root, PHP version and the site's run user, `file_read` on `.env` for `DB_DATABASE`, `database_info` to confirm that database exists. Never infer the database name from the domain, and never assume the owner.

2. **Inspect and preview in one batch** — a single `run_shell`: `git status --porcelain`, `git rev-parse HEAD` for the rollback point, `git fetch`, the diff of `database/migrations`, then

   ```
   git checkout origin/main -- database/migrations
   php artisan migrate:status
   php artisan migrate --pretend --force
   git checkout HEAD -- database/migrations
   git status --porcelain
   ```

   The temporary checkout is what makes the preview true. `--pretend` reads the migration files on disk, so run before the merge it prints the SQL of the *old* pending migrations — the user approves one thing and a different thing executes. Reverting the directory and re-checking `git status` afterwards proves the tree is back where it started.

3. **Gate on that SQL.** Show it, name any `dropColumn`, `renameColumn` or type change against a table holding live records, and wait. Everything after this point mutates the server; nothing before it has.

4. **`database_backup`, then wait for it.** REQUIRED before any migration. The call returns immediately; poll `system_tasks` until the backup is no longer listed as running. Migrating against a half-written dump leaves you with no rollback at the exact moment you need one.

5. **`php artisan down`** — Laravel's own maintenance page, not `site_set_status`, which takes the vhost down at the panel level and the maintenance page with it.

6. **Build batch** — one `run_shell`: `git merge --ff-only origin/main`, `composer install --no-dev --optimize-autoloader --no-interaction`, `npm ci`, `npm run build`. Fast-forward only, so a diverged tree fails loudly instead of merging silently. `timeout: 600`.

7. **Migrate batch** — `php artisan migrate --force`, alone in its own call so a failure here is unambiguous. On failure stop: no caches, no restart, no `artisan up`. Leave the site in maintenance mode, report the error with the backup from step 4 and the commit from step 2, and let the user choose.

8. **Finish batch** — `config:cache`, `route:cache`, `view:cache`, and `queue:restart` if workers run.

9. **Fix ownership, and do it here.** REQUIRED after step 8, not before it. `run_shell` runs as root, so every file the build and the cache rebuild produced is root-owned — including the `bootstrap/cache/*.php` that `config:cache` writes. Fixing ownership before the last root-writing command just gets undone by it, and PHP-FPM then cannot write `storage/` or `bootstrap/cache`, so every request 500s after an otherwise clean release. `file_set_permissions` on `storage` and `bootstrap/cache` with the owner from step 1.

10. **`service_restart`** with `mode="reload"` on the site's PHP-FPM service to drop the old OPcache bytecode, then `php artisan up`.

11. **Verify** — `site_logs`, and a `curl` for the status code. An `artisan up` that succeeded does not mean the site serves.

## Never put a secret in a command

The audit log redacts by argument name, and `run_shell`'s argument is `command`. A password inside the command line is written to the audit log verbatim and shown in the confirmation summary. Put secrets in `.env` with `file_write` instead.

## If the shell tier is off

`run_shell` is opt-in and may be absent from the tool list entirely. Then nothing above involving git, composer or npm is possible: build the release elsewhere, upload the artifact, and use `file_extract` plus the write-tier steps.

## Common mistakes

| Mistake | What happens |
|---|---|
| One `run_shell` per command | A confirmation round-trip each. Batch with `&&`. |
| No `; echo "EXIT:$?"` | A failed build reads as a successful one and the release continues on top of it. |
| Migrating right after `database_backup` returns | The dump is still being written. No rollback point. |
| Re-running a command that returned `finished: false` | It is still running. Two concurrent builds in the same directory. |
| Fixing ownership before the cache rebuild | `config:cache` re-creates `bootstrap/cache` as root. Site returns 500 on every request. |
| `migrate --pretend` before the merge | Prints the old pending migrations. The user approves SQL that never runs. |
| `site_set_status` for downtime | Panel-level stop instead of a 503 maintenance page. |
| Secret on the `run_shell` command line | Written to the audit log in clear text. |
