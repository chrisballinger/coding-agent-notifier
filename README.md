# coding-agent-notifier

[![CI](https://github.com/chrisballinger/coding-agent-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/chrisballinger/coding-agent-notifier/actions/workflows/ci.yml)

> In case it's not obvious, this is all slop. Use at your own risk.

Ping yourself on Slack or Discord when a coding agent — Claude Code,
Codex — needs your attention, but only when you're actually away from
the keyboard.

Fills the gap left by Claude Code Remote in the Claude iOS app, which
doesn't yet support push notifications. Also covers Claude Code
deployments running against Amazon Bedrock, where Claude Code Remote
isn't available at all.

Built for the workflow where you kick off an agent, switch to another
task, and want to know *the moment* it asks for approval or finishes —
without having to babysit the terminal.

## What triggers a ping

| Event                         | Claude Code                                       | Codex                                   |
| ----------------------------- | ------------------------------------------------- | --------------------------------------- |
| Tool-approval prompt          | `PermissionRequest`, `Notification:permission_prompt` | `PermissionRequest` hook            |
| Agent waiting for your answer | `Notification:idle_prompt`                        | not emitted by Codex today              |
| MCP server asking for input   | `Notification:elicitation_dialog`                 | —                                       |
| Turn complete                 | `Stop`                                            | `notify` (`agent-turn-complete`), `Stop` |

By default a ping only fires when your machine has been idle past 60s
**or** the agent's terminal isn't frontmost — see
[CONFIGURATION.md](CONFIGURATION.md#top-level) for the gating modes.

## Quick start

Two flavors, depending on how much you want to set up. Both require
Python 3.11+ and [`uv`](https://github.com/astral-sh/uv) (pipx / pip
also work).

### A) Webhook — one-way notifications

Easiest path: incoming-webhook URL, no daemon, no buttons. Works on any
OS where Python runs.

```bash
uv tool install 'coding-agent-notifier'
agent-notify config init
$EDITOR "$(agent-notify config path)"   # add a [slack.workspaces.default] block with webhook_url
agent-notify install claude-code        # merges hooks into ~/.claude/settings.json
agent-notify install codex              # writes ~/.codex/config.toml + hooks.json
agent-notify test --force               # synthetic ping to confirm round-trip
```

Discord is the same shape (`webhook_url` under `[sinks.discord]`).

### B) Slack bot — phone-tap approvals

Adds blocking PermissionRequest approval with Approve / Deny / Option
buttons that work from your phone, plus a Show more / Show less toggle
for long messages. macOS only (uses launchd to supervise the daemon
and macOS Keychain for tokens).

```bash
uv tool install 'coding-agent-notifier[slack-bot]'
agent-notify config init
agent-notify slack add                  # interactive wizard: tokens → Keychain
agent-notify install slack-bot          # writes Claude Code hook + launchd plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/app.coding-agent-notifier.daemon.plist
agent-notify slack test default         # smoke-test message DM'd to you
agent-notify doctor                     # confirms config, daemon, route match
```

Create the Slack App from the bundled manifest at
`docs/slack-app-manifest.yaml`
([api.slack.com/apps](https://api.slack.com/apps) → Create New App →
From a manifest), then copy the `xoxb-…` bot token and generate an
`xapp-…` app-level token (`connections:write` scope) before running
`agent-notify slack add`.

The full button-flow reference (option pickers, multi-question
AskUserQuestion, suggestion buttons, deny-with-reason modal, resolved
message wording) lives in
[CONFIGURATION.md → Phone-tap approvals](CONFIGURATION.md#phone-tap-approvals--visual-reference).

### Compliance / locked-down mode

For shared workspaces, a corporate Slack, or anywhere the *content*
of an agent's tool calls cannot transit a third-party service:

```toml
[display]
verbosity = "minimal"                   # only the event title leaves the machine

[slack.workspaces.default]
channel = "@me"                         # bot-DM only, never a shared channel
actionable_approvals = true             # buttons still work in minimal mode
approver_user_ids = ["U0YOURID"]        # explicit allowlist
```

In `minimal` mode the title ("Claude Code needs approval") is the only
thing that posts — no tool name, no command, no project path, no
session ID. Buttons carry only an opaque UUID, so taps still work
end-to-end. See
[SECURITY.md → Data minimization](SECURITY.md#data-minimization-what-actually-transits-slack--discord)
for the full per-tier breakdown.

## Where state lives

```
~/.agent-notify/              (0700)
├── config.toml               (0600)   your config
├── secrets.toml              (0600, optional) — fill-in layer for tokens
├── state/                    (0700)   dedup markers, queued events, pending approvals
└── logs/                     (0700)   defer.log, daemon.log
```

## Security

Short version: the daemon never opens a listening socket — it connects
*outbound* to Slack via Socket Mode, so a browser tab on `localhost`
can't reach it. Click authors are checked against your
`approver_user_ids` allowlist before any state mutation. Tokens prefer
macOS Keychain (or `secrets.toml` on Linux) over env vars and inline
TOML. The blocking PermissionRequest hook fails *closed* (deny) on any
error — never silently approves.

Full threat model, secret-handling tradeoffs, and the per-tier data
minimization table are in [SECURITY.md](SECURITY.md). Vulnerability
reports go via [private security
advisory](https://github.com/chrisballinger/coding-agent-notifier/security/advisories/new).

## Configuration

Every knob is documented in [CONFIGURATION.md](CONFIGURATION.md).
Highlights:

- `[display]` — `verbosity` (`terse`/`normal`/`minimal`),
  coalesce window, message preview budget for the Show more toggle.
- `[summary]` — head/tail snippet of the agent's last message on
  `turn_complete` (purely local, no API keys).
- `[slack.workspaces.<name>]` — multi-workspace, with optional
  per-workspace `channel`, `actionable_approvals`, approver allowlist,
  `approval_timeout_seconds` (set to `0` to wait forever — leave the
  approval pending until you resolve it from any device).
- `[[routes]]` — per-repo routing. As soon as one route exists, routing
  becomes strict; unmatched cwds get nothing rather than falling back
  to the default workspace.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Slack DM arrives but **buttons spin forever** | Daemon isn't running — Socket Mode never receives the click. | `launchctl print gui/$(id -u)/app.coding-agent-notifier.daemon \| grep "active count"`. If `0`, see the next two rows. |
| Daemon logs `ModuleNotFoundError: No module named 'slack_sdk'` | `uv tool install` ran without the `[slack-bot]` extras. | `uv tool install --reinstall 'coding-agent-notifier[slack-bot]'`, then bounce the daemon. |
| `launchctl print` shows `last exit code = 78: EX_CONFIG` | Old plist used a bare `agent-notify` program name; launchd's PATH excluded `~/.local/bin`. | Re-run `agent-notify install slack-bot` (the install writes the absolute path now), then `bootout` + `bootstrap` to reload. |
| `agent-notify slack test default` says "posted to U…" but **no DM appears** | Bot DMed itself (App Home Messages tab). Manifest wasn't declaring `messages_tab_enabled`. | Slack admin → your app → **App Home** → toggle **Messages Tab** on. Or recreate the app from `docs/slack-app-manifest.yaml`. |
| Hook fires but **nothing reaches Slack** | Stale installed `agent-notify` binary in `~/.local/share/uv/tools/`. | `uv tool install --reinstall 'coding-agent-notifier[slack-bot]'`; confirm `which agent-notify` and `defer.log`'s `spawning=` agree. |
| Doctor reports `legacy plist present` | Pre-rename install leftover. | `agent-notify install slack-bot` removes it. |

`~/.agent-notify/logs/daemon.log` is the daemon's stdout/stderr;
`defer.log` is every hook fire and dispatch decision. Both are `0600`.

## Commands

| Command | What it does |
| --- | --- |
| `agent-notify --version` | Print the package version. |
| `agent-notify --debug …` | Lower the stderr log level to DEBUG. Combine with any subcommand. |
| `agent-notify hook --source {claude-code,codex}` | Reads a hook payload on stdin; invoked by the agents. |
| `agent-notify test [--force] [--kind …]` | Send a synthetic event end-to-end. |
| `agent-notify config init \| path` | Write / locate the config file. |
| `agent-notify install {claude-code,codex,slack-bot}` | Install hooks into the target agent (`slack-bot` also writes the launchd plist). |
| `agent-notify slack add \| list \| remove <name> \| test <name>` | Workspace setup wizard + management. |
| `agent-notify daemon` | Run the Slack Socket Mode listener (required when `actionable_approvals` is on). |
| `agent-notify doctor` | Summarize config, list workspaces with live `auth.test`, check daemon + route match. |

## Development

```bash
uv sync
uv run pytest          # 80% coverage gate enforced
uv run agent-notify --help
```

## License

MIT — see [LICENSE](LICENSE).
