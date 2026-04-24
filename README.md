# coding-agent-notifier

[![CI](https://github.com/chrisballinger/coding-agent-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/chrisballinger/coding-agent-notifier/actions/workflows/ci.yml)

Ping yourself on Slack or Discord when a coding agent — Claude Code, Codex — needs your attention, but only when you're actually away from the keyboard.

Built for the workflow where you kick off an agent, switch to another task, and want to know *the moment* it asks for approval or finishes — without having to babysit the terminal.

Duplicate-ping protection is built in: both agents fire two hooks for the same logical event (Claude Code: `PermissionRequest` + `Notification:permission_prompt`; Codex: `notify` + `Stop`), so the CLI collapses pairs within a 5-second window.

## What triggers a ping

| Event                               | Claude Code                                       | Codex                                   |
| ----------------------------------- | ------------------------------------------------- | --------------------------------------- |
| Tool-approval prompt                | `PermissionRequest`, `Notification:permission_prompt` | `PermissionRequest` hook              |
| Agent waiting for your answer       | `Notification:idle_prompt`                        | *(pending upstream)*                    |
| MCP server asking for input         | `Notification:elicitation_dialog`                 | —                                       |
| Turn complete                       | `Stop`                                            | `notify` (`agent-turn-complete`), `Stop` |

## Gating — why you won't get spammed

By default (`gating = "idle_or_background"`) a ping is only sent if **either**:
- your machine has been idle past `idle_threshold_seconds` (default 60), **or**
- the terminal / editor the agent is running in isn't frontmost.

Other modes: `idle_only`, `background_only`, `always`. Configurable per event — e.g. permission prompts default to `always` because approvals are urgent.

Gating degrades gracefully on non-macOS hosts and on failures: if we can't read idle time or the frontmost app, we fail open and send the ping.

## Install

Requires Python 3.11+ and [`uv`](https://github.com/astral-sh/uv) (or pipx / pip).

```bash
uv tool install --from . coding-agent-notifier
# or for development
uv sync
```

Write a starter config and drop in a Slack webhook:

```bash
agent-notify config init
$EDITOR "$(agent-notify config path)"
```

Wire up the agents:

```bash
agent-notify install claude-code   # merges into ~/.claude/settings.json
agent-notify install codex         # writes ~/.codex/config.toml + hooks.json
```

Both installers are idempotent and back up the target file before writing.

## Config

`~/.config/coding-agent-notifier/config.toml` (or `$XDG_CONFIG_HOME/coding-agent-notifier/config.toml`):

```toml
idle_threshold_seconds = 60
gating = "idle_or_background"   # idle_only | background_only | idle_or_background | always

[events.permission]
enabled = true
gating = "always"               # approvals are urgent

[events.idle_prompt]
enabled = true

[events.elicitation]
enabled = true

[events.turn_complete]
enabled = true

[sinks.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/…"
# Or use a bot token for DMs:
# bot_token = "xoxb-…"
# channel   = "@me"             # resolved via auth.test

[sinks.discord]
enabled = false
# webhook_url = "https://discord.com/api/webhooks/…"
```

## Per-repo routing

Need different projects pinging different Slack channels? Add `[[routes]]` blocks. The first whose `cwd` glob matches the hook's working directory wins; overrides merge on top of the base sink configs.

```toml
# Work projects → a shared #agents-work channel (bot token)
[[routes]]
cwd = "~/work/acme-*"
slack.channel = "#agents-work"

# An OSS project → its own webhook
[[routes]]
cwd = "~/oss/my-library"
slack.webhook_url = "https://hooks.slack.com/services/oss/…"

# Personal stuff stays quiet
[[routes]]
cwd = "~/personal/*"
slack.enabled = false
```

Patterns use `fnmatch`-style globs (`*`, `?`, `[abc]`) with `~` expansion. Use `agent-notify doctor` from inside a repo to confirm which route (if any) matches your current `cwd`.

## Commands

| Command                                | What it does                                                   |
| -------------------------------------- | -------------------------------------------------------------- |
| `agent-notify hook --source {claude-code,codex}` | Reads a hook payload on stdin; invoked by the agents. |
| `agent-notify test [--force] [--kind ...]`       | Sends a synthetic event end-to-end.                   |
| `agent-notify config init \| path`               | Write / locate the config file.                       |
| `agent-notify install {claude-code,codex}`       | Install hooks into the target agent.                  |
| `agent-notify doctor`                            | Summarize config, system state, install state.        |

## Development

```bash
uv sync
uv run pytest       # 80% coverage gate
uv run agent-notify --help
```

## License

MIT
