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

All state lives under a single dot directory — `~/.agent-notify/` by default, or wherever `AGENT_NOTIFY_HOME` points:

```
~/.agent-notify/              (0700)
├── config.toml               (0600)   your config
├── state/                    (0700)   dedup markers, queued events, pending approvals
└── logs/                     (0700)   defer.log, daemon.log
```

If you previously used `agent-notify` with state under `~/.config/coding-agent-notifier/` and `~/.cache/coding-agent-notifier/`, the next run auto-migrates the files into the new tree and prints a one-line notice. The old directories are left in place so you can inspect before removing them yourself.

`~/.agent-notify/config.toml`:

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
verbosity = "terse"             # terse | normal | minimal. See "Security" below.
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

## Security

`agent-notify` takes a defense-in-depth posture appropriate for a tool that sees approval prompts and, with the bot feature, relays them through third parties. Specifics:

**Where things live, and who can read them.** Everything under `~/.agent-notify/` is owner-only: directories are `0700`, files are `0600`. Writes happen through an `os.open(…, 0o600)` + atomic-replace helper that ignores the process umask, so a badly-configured shell can't leak state to group/world. The install flow writes `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.codex/hooks.json`, and the launchd plist the same way. Timestamped `.bak-*` backups inherit the strict mode of the original file.

**Config permission warning.** On load, if `config.toml` is group/world-readable AND contains any inline `webhook_url` / `bot_token` / `app_token` value (as opposed to an `_env` reference), `agent-notify` prints a loud stderr warning telling you to `chmod 600` it. It does *not* refuse to load — you may have reasons — but the warning is hard to miss.

**Token storage, in order of preference.**

1. Environment variable — config refers to it via `bot_token_env = "SLACK_BOT_TOKEN"`. Nothing secret on disk.
2. Inline in `config.toml` — acceptable only if the file is `0600` and on an encrypted disk (e.g. macOS FileVault).
3. We do *not* currently integrate with macOS Keychain — see "Out of scope" below.

**What goes into logs.** `logs/defer.log` records metadata only — approval IDs, session IDs (short), tool names, decisions — and is created at `0600`. `tool_input` contents are never logged. If you see a stack trace in the log, it's from our own code; no token or command body is printed there either.

**Verbosity and the payload that transits Slack / Discord.** The `display.verbosity` setting controls *how much* of each event actually leaves the machine:

| Mode      | Event title | Tool name | `tool_input` (commands / code) | Transcript snippet | Project path / session ID |
| --------- | :---------: | :-------: | :-----------------------------: | :----------------: | :-----------------------: |
| `normal`  |      ✓      |     ✓     |                ✓                |          ✓         |             ✓             |
| `terse`   |      ✓      |     ✓     |                ✓                |          ✓         |       ✓ (compact)         |
| `minimal` |      ✓      |     —     |                —                |          —         |             —             |

`minimal` mode exists for environments where the *content* of an agent's pending tool call — a command, a snippet of code, the name of a repo — cannot transit a third-party service. You still get a ping that tells you to glance at the terminal; the terminal is authoritative for what's waiting. Approve/deny buttons still work (they carry only an opaque UUID), and the Slack confirm dialog is stripped of tool-specific text.

**Fail-closed approvals.** The blocking `PreToolUse` hook (when `sinks.slack.actionable_approvals = true`) defaults to `deny` on any error path: Slack post failure, timeout, daemon crash, token absent. Rationale: a notifier that silently *approves* on failure is much worse than one that silently denies — the worst case is a one-line "denied" message and you re-run in the terminal.

**Safer example commands.** No destructive shell patterns (`rm -rf …`) appear in fixtures, examples, screenshots, or the `agent-notify test --dangerous` synthetic event — on the theory that a user who copies a command out of a notification into a terminal should not be harmed by it. Placeholder commands use `https://example.invalid/...` (a reserved TLD that never resolves) so `curl | bash`-style examples are cosmetically dangerous but cannot actually do anything.

### Out of scope (today)

- **macOS Keychain token storage.** Future pass — `security find-generic-password` or PyObjC. For now, env vars are the right answer.
- **Encryption at rest.** Rely on macOS FileVault.
- **Audit log of approve/deny decisions.** Separate feature — file if you want it.
- **End-to-end encryption of push payloads.** See `docs/plans/ios-live-activities.md` for a native-iOS design that does E2EE; Slack itself is in-band encrypted but Slack-readable.

## Commands

| Command                                | What it does                                                   |
| -------------------------------------- | -------------------------------------------------------------- |
| `agent-notify hook --source {claude-code,codex}` | Reads a hook payload on stdin; invoked by the agents. |
| `agent-notify test [--force] [--kind ...]`       | Sends a synthetic event end-to-end.                   |
| `agent-notify config init \| path`               | Write / locate the config file.                       |
| `agent-notify install {claude-code,codex,slack-bot}` | Install hooks into the target agent (slack-bot also writes the launchd plist for the daemon). |
| `agent-notify daemon`                                 | Run the Slack Socket Mode listener (required when `actionable_approvals` is on).              |
| `agent-notify doctor`                            | Summarize config, system state, install state.        |

## Development

```bash
uv sync
uv run pytest       # 80% coverage gate
uv run agent-notify --help
```

## License

MIT
