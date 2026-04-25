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

With actionable approvals enabled (Slack bot install), the permission DM also carries one-tap buttons — Approve/Deny on most tools, one button per option for `AskUserQuestion`, plus per-suggestion buttons that approve **and** extend your allowlist in a single tap. See [Phone-tap approvals](#phone-tap-approvals).

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
# or with the actionable-Slack daemon (Socket Mode listener + buttons):
uv tool install --from . 'coding-agent-notifier[slack-bot]'
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

[slack.workspaces.default]
enabled = true
# Bot token: pick ONE source (Keychain is preferred — see §Credential storage below)
bot_token_keychain = "default:bot_token"
# bot_token_env    = "SLACK_BOT_TOKEN"
# bot_token        = "xoxb-…"

# For interactive approvals (approve/deny buttons, blocking PermissionRequest hook):
# app_token_keychain   = "default:app_token"
# interactive          = true
# actionable_approvals = true
# approver_user_ids    = ["U0YOURID"]   # required for shared channels
channel = "@me"                          # DM with the bot (secure default)

[sinks.discord]
enabled = false
# webhook_url = "https://discord.com/api/webhooks/…"
```

The interactive setup wizard (`agent-notify slack add`) writes this block for you, stores tokens in macOS Keychain, and verifies via Slack's `auth.test`. Run it once per workspace — multi-workspace is supported by naming each block `[slack.workspaces.<name>]` and referencing names from `[[routes]]`. Legacy `[sinks.slack]` still parses as the implicit `default` workspace for backwards compatibility.

> **Migration note (existing Slack App installs):** the bundled manifest (`docs/slack-app-manifest.yaml`) now declares an `app_home` block with `messages_tab_enabled: true` so the no-approver fallback DM is visible. If your Slack App was created from an earlier version of the manifest, either re-create it from the updated YAML *or* enable the toggle manually: in your Slack App's admin, **App Home** → switch **Messages Tab** on. Without this, posts to the bot's own user_id (the fallback when `approver_user_ids` is empty) return `ok: true` but are functionally invisible.

### Phone-first defaults

On iOS the push preview is tight, so the default layout is optimized for a glance:

- **Terse verbosity** (default) drops the repeated Project/Session/Tool/App field block and shows `project · session · app` as a compact footer instead. Set `display.verbosity = "normal"` for the verbose grid.
- **Coalesce window** (default 2.5s) defers `turn_complete` briefly so a follow-up `idle_prompt` can suppress it — you get one "waiting on you" ping rather than "finished a turn" + "waiting on you" back-to-back. Set `display.coalesce_window_seconds = 0` to disable.
- **Turn-complete snippet** (default on) reads the last assistant message from the transcript and shows its head + tail. Purely local — no API keys, no network.
- **AskUserQuestion rendering** formats the question + options as a bulleted list rather than raw JSON.

## Phone-tap approvals

When `actionable_approvals = true` is set on a Slack workspace and the daemon is running, the permission DM that lands on your phone is interactive — not just a notification.

**Setup (one-time):**

```bash
uv tool install --from . 'coding-agent-notifier[slack-bot]'   # base + slack-sdk
agent-notify install slack-bot                                # writes Claude Code hook + launchd plist
agent-notify slack add                                        # interactive wizard: tokens to Keychain, config block
launchctl bootout gui/$(id -u)  ~/Library/LaunchAgents/com.chrisballinger.agent-notify-daemon.plist  # if previously loaded
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.chrisballinger.agent-notify-daemon.plist
```

The daemon connects to Slack over Socket Mode (outbound WebSocket — no public port). Verify it's up:

```bash
launchctl print gui/$(id -u)/com.chrisballinger.agent-notify-daemon | grep -E 'state|active count'
agent-notify slack test default   # posts a smoke message to your DM
```

**Visual semantics.** The sidebar color reflects what the message is asking of you:

| Tier   | Color  | When                                                             |
| ------ | ------ | ---------------------------------------------------------------- |
| Green  | `#2eb67d` | Informational / done / question (turn_complete, idle_prompt, AskUserQuestion) |
| Yellow | `#ecb22e` | Action required (permission requests on real tool calls)        |
| Red    | `#a30f18` | Danger override — `tool_input` matched a destructive Bash pattern |

**Button kinds.** Three flavors of interactivity, picked automatically from the tool:

- **Approve / Deny** — the default for any tool needing permission. Approve has a confirmation dialog (an accidental lock-screen tap shouldn't run a Bash command); Deny is one-tap.
- **Option buttons** — when the tool is `AskUserQuestion`, one button per option label. The first option whose label contains `(Recommended)` gets a filled green CTA. Multi-question questions render as separate actions blocks; tapping records a partial answer (the message updates with a ✓ on each answered question), and the approval finalizes only when every question has an entry. The hook then pre-fills the answers via `decision.updatedInput`, so the AskUserQuestion tool returns immediately without prompting in the terminal.
- **Suggestion buttons** — when Claude Code attaches `permission_suggestions` to the request (e.g. *"add `Bash(curl:*)` to localSettings"*), each one becomes an extra button below Approve/Deny. Tapping resolves the approval as `allow` and emits `decision.updatedPermissions` so the rule edit is applied immediately — extending your allowlist in one tap.

**Resolved-message wording** updates in place via `chat.update`:

| Outcome                                | Header                                              |
| -------------------------------------- | --------------------------------------------------- |
| Plain Approve                          | ✅ Approved by @you                                  |
| Plain Deny                             | 🚫 Denied by @you                                    |
| Single-question option click           | ✅ Selected `<label>` by @you                        |
| Multi-question all-questions answered  | ✅ Answered by @you (+ Q→A summary block)            |
| Suggestion click                       | ✅ Approved & applied `<rule>` by @you               |
| Timeout / failed Slack post            | ⏳ Timed out — denied                                |

The daemon authorizes click authors against `approver_user_ids` (or `approver_user_groups` if you set them). If both lists are empty, the click is only honored when it came from a 1:1 DM — defense-in-depth against misconfigured shared channels.

## Per-repo routing

Need different projects pinging different Slack workspaces or channels? Add `[[routes]]` blocks. The first whose `cwd` glob matches the hook's working directory wins; overrides merge on top of the selected workspace.

```toml
# Two workspaces defined up top via `agent-notify slack add --name <name>`
# [slack.workspaces.home] and [slack.workspaces.work] — tokens live in Keychain.

# Work repos route to the `work` workspace + a shared channel
[[routes]]
cwd = "~/work/acme-*"
slack.workspace = "work"
slack.channel = "#agents-acme"

# OSS stays on the personal workspace
[[routes]]
cwd = "~/oss/my-library"
slack.workspace = "home"

# Personal stuff stays quiet
[[routes]]
cwd = "~/personal/*"
slack.enabled = false
```

`slack.workspace = "<name>"` picks the base config; any other `slack.*` key patches a single field on top. Without `slack.workspace`, the route uses the `default` workspace (or the legacy `[sinks.slack]` block if you haven't migrated yet).

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

**Credential storage, in order of preference.**

Each token (bot_token, app_token, webhook_url) resolves from the first matching source:

1. **`bot_token_keychain = "default:bot_token"`** — macOS Keychain via `/usr/bin/security`. Recommended for Mac users; `agent-notify slack add` writes here by default. Keychain entries outlive config resets and are protected by the user's login keychain ACL. *Trade-off:* `security add-generic-password` takes the password on argv during the brief subprocess lifetime — visible to `ps` during that window. Acceptable on a single-user Mac; if that matters on your host, use the secrets-file route instead.

2. **`secrets.toml` (sibling of `config.toml`, 0600 enforced)** — separate TOML file that fills in any keys `config.toml` left unset. Permissions are *enforced* (not just warned): a group/world-readable `secrets.toml` refuses to load. Works on Linux and macOS; the best option when you want to commit `config.toml` to dotfiles but keep secrets out. *Trade-off:* relies on whatever disk encryption you have (FileVault, LUKS, etc.); no per-secret ACL like Keychain.

3. **`bot_token_env = "SLACK_BOT_TOKEN"`** — environment variable. Cross-platform, works in CI. *Trade-off:* env vars leak via process listings (`ps e`), subprocess inheritance, and shell rc-file commits. Prefer Keychain or `secrets.toml` on a developer machine.

4. **Inline `bot_token = "xoxb-…"`** — discouraged. Acceptable only when `config.toml` is 0600 on an encrypted disk; loud stderr warning fires on every load if the file is group/world-readable AND contains an inline secret.

If `bot_token_keychain` is configured but the account is missing or the Keychain subprocess fails, config load fails loudly rather than silently falling through — see `keychain.py`'s module docstring for the rationale.

**What goes into logs.** `logs/defer.log` records metadata only — approval IDs, session IDs (short), tool names, decisions — and is created at `0600`. `tool_input` contents are never logged. If you see a stack trace in the log, it's from our own code; no token or command body is printed there either.

**Verbosity and the payload that transits Slack / Discord.** The `display.verbosity` setting controls *how much* of each event actually leaves the machine:

| Mode      | Event title | Tool name | `tool_input` (commands / code) | Transcript snippet | Project path / session ID |
| --------- | :---------: | :-------: | :-----------------------------: | :----------------: | :-----------------------: |
| `normal`  |      ✓      |     ✓     |                ✓                |          ✓         |             ✓             |
| `terse`   |      ✓      |     ✓     |                ✓                |          ✓         |       ✓ (compact)         |
| `minimal` |      ✓      |     —     |                —                |          —         |             —             |

`minimal` mode exists for environments where the *content* of an agent's pending tool call — a command, a snippet of code, the name of a repo — cannot transit a third-party service. You still get a ping that tells you to glance at the terminal; the terminal is authoritative for what's waiting. Approve/deny buttons still work (they carry only an opaque UUID), and the Slack confirm dialog is stripped of tool-specific text.

**Fail-closed approvals.** The blocking `PermissionRequest` hook (when `sinks.slack.actionable_approvals = true`) defaults to `deny` on any error path: Slack post failure, timeout, daemon crash, token absent. Rationale: a notifier that silently *approves* on failure is much worse than one that silently denies — the worst case is a one-line "denied" message and you re-run in the terminal. The hook only fires when Claude Code was about to prompt the user — auto-allowed tools (allowlist, sandbox) pass through untouched.

**Safer example commands.** No destructive shell patterns (`rm -rf …`) appear in fixtures, examples, screenshots, or the `agent-notify test --dangerous` synthetic event — on the theory that a user who copies a command out of a notification into a terminal should not be harmed by it. Placeholder commands use `https://example.invalid/...` (a reserved TLD that never resolves) so `curl | bash`-style examples are cosmetically dangerous but cannot actually do anything.

### Out of scope (today)

- **Linux-native secret stores.** On Linux / WSL use `secrets.toml` or env vars — we don't integrate with `libsecret` / GNOME Keyring today.
- **Encryption at rest.** Rely on macOS FileVault / LUKS / dm-crypt.
- **Audit log of approve/deny decisions.** Separate feature — file if you want it.
- **End-to-end encryption of push payloads.** See `docs/plans/ios-live-activities.md` for a native-iOS design that does E2EE; Slack itself is in-band encrypted but Slack-readable.

## Troubleshooting

| Symptom                                                                     | Likely cause                                                                                                  | Fix                                                                                                                                                   |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Slack DM arrives but **buttons spin forever**                               | Daemon isn't running — Socket Mode never receives the click.                                                  | `launchctl print gui/$(id -u)/com.chrisballinger.agent-notify-daemon \| grep "active count"`. If `0`, see the next two rows.                          |
| Daemon logs `ModuleNotFoundError: No module named 'slack_sdk'`              | `uv tool install` was run **without** the `[slack-bot]` extras, so the daemon can't import its dependencies.  | `uv tool install --from . --reinstall 'coding-agent-notifier[slack-bot]'` (note the extras), then bounce the daemon.                                  |
| `launchctl print` shows `last exit code = 78: EX_CONFIG`, `daemon.log` empty | Old plist used a bare `agent-notify` program name; launchd's PATH excluded `~/.local/bin`.                    | Re-run `agent-notify install slack-bot` (the install now writes the absolute path), then `launchctl bootout` + `bootstrap` to reload.                 |
| `agent-notify slack test default` says "posted to U…" but **no DM appears** | Bot DMed itself (App Home Messages tab). Manifest wasn't declaring `messages_tab_enabled` at app-create time.  | In Slack admin → your app → **App Home** → toggle **Messages Tab** on. Or recreate the app from the updated `docs/slack-app-manifest.yaml`.           |
| Reads (or other auto-allowed tools) suddenly **prompt for permission**       | Your hook command emits `permissionDecision: "ask"` somewhere — that overrides allowlists / sandbox.          | Should not happen with this version. If it does, check `~/.claude/settings.json` for stale `PreToolUse` blocks pointing at `agent-notify hook`.       |
| Hook fires but **nothing reaches Slack**                                    | Likely a stale installed `agent-notify` binary in `~/.local/share/uv/tools/`. The hook spawns the installed copy, not your dev source. | `uv tool install --from . --reinstall 'coding-agent-notifier[slack-bot]'` and confirm `which agent-notify` and `defer.log`'s `spawning=` agree.       |
| Multi-question AskUserQuestion **only renders Q1**                          | Old version. Multi-question support shipped in this release — just upgrade and reinstall.                     | `uv tool install --from . --reinstall 'coding-agent-notifier[slack-bot]'`.                                                                            |

`~/.agent-notify/logs/daemon.log` is the daemon's stdout/stderr; `defer.log` is every hook fire and dispatch decision. Both are `0600` and owner-only.

## Commands

| Command                                | What it does                                                   |
| -------------------------------------- | -------------------------------------------------------------- |
| `agent-notify hook --source {claude-code,codex}` | Reads a hook payload on stdin; invoked by the agents. |
| `agent-notify test [--force] [--kind ...]`       | Sends a synthetic event end-to-end.                   |
| `agent-notify config init \| path`               | Write / locate the config file.                       |
| `agent-notify install {claude-code,codex,slack-bot}` | Install hooks into the target agent (slack-bot also writes the launchd plist for the daemon). |
| `agent-notify slack {add,list,remove,test}`           | Manage Slack workspaces — the `add` wizard stores tokens in Keychain and writes the config block. |
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
