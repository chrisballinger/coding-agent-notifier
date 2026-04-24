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

[display]
verbosity = "terse"             # terse | normal. terse drops the Project/Session/Tool/App grid in favor of a compact footer.
coalesce_window_seconds = 2.5   # hold a `turn_complete` this long so a follow-up `idle_prompt` can cancel it; 0 disables.

[summary]
enabled = true                  # include a head/tail snippet of the agent's last message on `turn_complete`
head_chars = 250
tail_chars = 250

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

### Phone-first defaults

On iOS the push preview is tight, so the default layout is optimized for a glance:

- **Terse verbosity** (default) drops the repeated Project/Session/Tool/App field block and shows `project · session · app` as a compact footer instead. Set `display.verbosity = "normal"` for the verbose grid.
- **Coalesce window** (default 2.5s) defers `turn_complete` briefly so a follow-up `idle_prompt` can suppress it — you get one "waiting on you" ping rather than "finished a turn" + "waiting on you" back-to-back. Set `display.coalesce_window_seconds = 0` to disable.
- **Turn-complete snippet** (default on) reads the last assistant message from the transcript and shows its head + tail. Purely local — no API keys, no network.
- **AskUserQuestion rendering** formats the question + options as a bulleted list rather than raw JSON.

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

### Strict routing — no cross-project leakage

**As soon as any `[[routes]]` entry exists, routing becomes strict**: if the hook's `cwd` doesn't match *any* route, the notification is silently skipped (with a one-line stderr note) instead of falling back to `[sinks.slack]`. This prevents the hazard where you clone a new repo you forgot to route, and its events accidentally ping the channel of a different project.

If you *want* a catch-all, add one explicitly at the end — it's just a route:

```toml
[[routes]]
cwd = "~/work/acme-*"
slack.webhook_url = "https://hooks.slack.com/services/work/…"

[[routes]]
cwd = "*"                                               # catch-all
slack.webhook_url = "https://hooks.slack.com/services/personal/…"
```

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
