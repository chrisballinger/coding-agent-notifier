# Security policy

`coding-agent-notifier` is a local, single-user tool that bridges a coding
agent (Claude Code, Codex) on your machine to your phone via Slack/Discord.
Read this document before running it on a shared workstation, in a CI
environment, or against a workspace you don't own.

## Reporting a vulnerability

Please **do not file a public GitHub issue for security problems.** Open a
[private security advisory](https://github.com/chrisballinger/coding-agent-notifier/security/advisories/new)
on this repository instead. We aim to respond within one week and to ship a
fix or coordinated disclosure within 30 days for confirmed issues.

When reporting, include:

- Steps to reproduce (config snippets, hook payloads — redact tokens).
- The affected version (`agent-notify --version`).
- Your assessment of impact and any suggested mitigation.

## Threat model

### What we defend against

- A misconfigured Slack app that posts approval messages to a channel
  without a configured allowlist (rejected at config-load time).
- A click on an approval button by a Slack user who isn't in
  `approver_user_ids` / `approver_user_groups` (ignored, ephemeral
  rejection sent back).
- A failure in any of the dispatch paths — Slack post, FIFO read, daemon
  crash, network timeout — fails closed (deny) for the
  `actionable_approvals` flow.
- Group/world-readable `config.toml` carrying inline secrets (warned
  loudly; `secrets.toml` hard-fails).
- Writes to state files (`~/.agent-notify/state/`) and logs go through
  `paths.write_secure` with `0600` perms, regardless of process umask.
- Unrecognised `permission_suggestions` entries in a hook payload
  (validated structurally and dropped before they can reach
  `decision.updatedPermissions`).

### What we explicitly don't defend against

- **A compromised local user account.** We don't try to defend against an
  attacker who already runs code as the installing user. They can read
  Keychain, edit `config.toml`, replace the `agent-notify` binary on the
  PATH, and forge state files. The trust boundary is your macOS user
  account, not the agent process.
- **A compromised coding agent (Claude Code / Codex itself).** If the
  agent harness is compromised, it can craft any hook payload it wants;
  we validate `permission_suggestions` shape but cannot prevent a hostile
  harness from omitting suggestions or claiming a benign tool name for a
  dangerous tool input.
- **Forgery of Slack websocket payloads.** Slack Socket Mode is an
  outbound-only WebSocket: the daemon connects out to Slack and Slack
  authenticates the connection with the `xapp-` app token. There is no
  inbound HTTP listener to forge requests against. If your `xapp-` token
  leaks, an attacker holding it can drive the daemon — rotate the token
  via Slack's app management page and the existing daemon will reject
  the new traffic.

## Secret handling

### macOS Keychain (recommended)

`agent-notify slack add` stores Slack bot and app tokens in macOS Keychain
under accounts named `agent-notify:<workspace>:bot_token` and
`agent-notify:<workspace>:app_token`. The wizard shells out to
`/usr/bin/security add-generic-password -w <token>`. **Known tradeoff:**
for the duration of that subprocess, the token is visible in `ps` to
processes that can see your argv. macOS' `security(1)` does not have a
stdin mode, so we accept this on single-user Macs as a documented
limitation. On shared / multi-user macOS hosts, prefer the env-var path
below.

### Environment variables

Each token field accepts a `<field>_env = "VAR_NAME"` resolver in
`config.toml`. Use this on shared machines, in CI, or when you want
tokens managed by an external secret store (1Password CLI, AWS SSM,
direnv, etc.). The env var is read once at config-load time.

### Inline tokens

Inline values in `config.toml` work but trigger a loud stderr warning if
the file isn't `0600`. Prefer Keychain or env vars for anything you'd be
sad to leak.

### `secrets.toml`

A sibling `secrets.toml` (in the same dot directory) is treated as a
fill-in-missing layer over `config.toml`. Permissions are **enforced**
at `0600` — a group-readable `secrets.toml` aborts the load rather than
warning. This is the right path for "I want to commit `config.toml` to
my dotfiles repo without committing tokens."

## Filesystem layout

```
~/.agent-notify/              (0700)
├── config.toml               (0600)
├── secrets.toml              (0600, optional)
├── state/                    (0700)
│   ├── dedup.json
│   ├── pending/
│   └── approvals/            (per-approval JSON + FIFO, 0600)
└── logs/                     (0700)
    ├── defer.log             (hook audit trail, append-only)
    └── daemon.log            (daemon stderr; rotated 10 MiB × 3)
```

`paths.ensure_dir` re-tightens directory modes on every access — if a
loose `state/` somehow ends up at `0755`, we chmod it back to `0700`
rather than silently leaving the hole.

## Backup files

The installers create timestamped `.bak-<ts>` files next to anything they
modify (`~/.claude/settings.json`, `~/.codex/config.toml`,
`~/.codex/hooks.json`, the launchd plist). These backups inherit the
original file's mode (capped at `0600`). They are plaintext copies — if
you ever wrote inline tokens into `~/.claude/settings.json` and then
rotated them, the old token survives in any `.bak-` file until you
delete it.

## launchd daemon

The `agent-notify install slack-bot` flow writes a launchd LaunchAgent
plist at `~/Library/LaunchAgents/com.chrisballinger.agent-notify-daemon.plist`
(mode `0600`). The plist:

- Runs as the installing user (no `sudo`, no system daemon).
- `KeepAlive=true` with `ThrottleInterval=10` so a fast-failing daemon
  cannot fork-bomb itself if Slack rejects the WebSocket.
- Logs stderr to `~/.agent-notify/logs/daemon.log` (which the daemon
  itself rotates at 10 MiB × 3 backups).

## Code signing / notarization

`coding-agent-notifier` is distributed as a Python package via PyPI;
Gatekeeper does not enforce signatures on Python scripts run by a
developer's interpreter. Code signing of the entrypoint is therefore
not meaningful for `pip install`-shaped distribution. A future
notarized `.app` bundle may follow (`briefcase`-based, distributed via
Homebrew Cask) — see the project README for status.
