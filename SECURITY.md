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

### Network attack surface

The daemon **never opens a listening socket** — no HTTP server, no
localhost port, nothing for a browser tab or rogue local process to
`fetch()` or DNS-rebind into. Its only network presence is an *outbound*
WebSocket to Slack via Socket Mode (`wss-primary.slack.com`).
Approve/Deny clicks travel back over that same outbound connection:
Slack authenticates the click sender (real Slack `user_id`); the daemon
then re-checks against your `approver_user_ids` / `approver_user_groups`
allowlist before mutating any state. Toggle clicks (Show more / Show
less) hit the same gate. Empty allowlists are accepted only when
`channel = "@me"` (DM with the bot) — anything else fails at
config-load with a `ConfigError`. There is no shell-injection path:
every `subprocess.run` in the codebase uses argv-style invocation;
`shell=True` does not appear anywhere.

### What we defend against

- A misconfigured Slack app that posts approval messages to a channel
  without a configured allowlist (rejected at config-load time).
- A click on an approval button by a Slack user who isn't in
  `approver_user_ids` / `approver_user_groups` (ignored, ephemeral
  rejection sent back).
- A failure in any of the dispatch paths — Slack post, FIFO read, daemon
  crash, network timeout — fails closed (deny) for the
  `actionable_approvals` flow. Worst case is a one-line "denied"
  message and you re-run in the terminal; silently approving on failure
  would be much worse.
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

Each token (`bot_token`, `app_token`, `webhook_url`) resolves from the
first matching source in this order. Use the highest-trust one your
environment supports.

### 1. macOS Keychain (recommended on macOS)

`bot_token_keychain = "default:bot_token"`. `agent-notify slack add`
stores Slack bot and app tokens in macOS Keychain under accounts named
`agent-notify:<workspace>:bot_token` and
`agent-notify:<workspace>:app_token`. The wizard shells out to
`/usr/bin/security add-generic-password -w <token>`. Keychain entries
outlive config resets and are protected by the user's login keychain
ACL.

**Known tradeoff:** for the duration of that subprocess, the token is
visible in `ps` to processes that can see your argv. macOS'
`security(1)` does not have a stdin mode, so we accept this on
single-user Macs as a documented limitation. On shared / multi-user
macOS hosts, prefer the `secrets.toml` route below.

If `bot_token_keychain` is configured but the account is missing or the
Keychain subprocess fails, config load fails loudly rather than silently
falling through.

### 2. `secrets.toml` (recommended on Linux / WSL / shared hosts)

A sibling `secrets.toml` (in the same dot directory) is treated as a
fill-in-missing layer over `config.toml`. Permissions are **enforced**
at `0600` — a group-readable `secrets.toml` aborts the load rather than
warning. This is the right path for "I want to commit `config.toml` to
my dotfiles repo without committing tokens", and the cleanest secret
store on Linux today.

### 3. Environment variables

`bot_token_env = "SLACK_BOT_TOKEN"`. Cross-platform, works in CI.
*Tradeoff:* env vars leak via process listings (`ps e`), subprocess
inheritance, and shell rc-file commits. Prefer Keychain or
`secrets.toml` on a developer machine.

### 4. Inline tokens (discouraged)

Inline values in `config.toml` work but trigger a loud stderr warning if
the file isn't `0600`. Acceptable only on encrypted disks; prefer
anything else above.

### Logging

`logs/defer.log` records metadata only — approval IDs, session IDs
(short), tool names, decisions — and is created at `0600`. `tool_input`
contents are never logged. If you see a stack trace in the log, it's
from our own code; no token or command body is printed there either.

## Data minimization (what actually transits Slack / Discord)

The `display.verbosity` setting controls how much of each event leaves
the machine. Pick the tier that matches your environment's compliance
posture:

| Mode      | Event title | Tool name | `tool_input` (commands / code) | Transcript snippet | Project path / session ID |
| --------- | :---------: | :-------: | :-----------------------------: | :----------------: | :-----------------------: |
| `normal`  |      ✓      |     ✓     |                ✓                |          ✓         |             ✓             |
| `terse`   |      ✓      |     ✓     |                ✓                |          ✓         |       ✓ (compact)         |
| `minimal` |      ✓      |     —     |                —                |          —         |             —             |

`minimal` mode exists for environments where the *content* of an
agent's pending tool call — a command, a snippet of code, the name of a
repo — cannot transit a third-party service. You still get a ping that
tells you to glance at the terminal; the terminal is authoritative for
what's waiting. Approve/deny buttons still work (they carry only an
opaque UUID), and the Slack confirm dialog is stripped of tool-specific
text.

For shared / corporate workspaces, the safest combination is:

```toml
[display]
verbosity = "minimal"

[slack.workspaces.default]
channel = "@me"                        # bot-DM only, never a shared channel
actionable_approvals = true            # buttons still work in minimal mode
approver_user_ids = ["U0YOURID"]       # explicit allowlist
```

## Safer example commands

No destructive shell patterns (`rm -rf …`) appear in fixtures, examples,
screenshots, or the `agent-notify test --dangerous` synthetic event —
on the theory that a user who copies a command out of a notification
into a terminal should not be harmed by it. Placeholder commands use
`https://example.invalid/...` (a reserved TLD that never resolves) so
`curl | bash`-style examples are cosmetically dangerous but cannot
actually do anything.

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
plist at `~/Library/LaunchAgents/app.coding-agent-notifier.daemon.plist`
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
