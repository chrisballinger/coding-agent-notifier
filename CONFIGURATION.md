# Configuration reference

Every knob in `~/.agent-notify/config.toml`, with defaults and the
intent behind each. The file is plain TOML; `agent-notify config init`
writes a fresh one with the defaults pre-filled and inline comments.

The minimum quick-start configs live in [README.md](README.md). Read
this doc when you want to tune behavior, add multi-workspace routing,
or lock things down for a corporate environment (which is also covered
in [SECURITY.md](SECURITY.md#data-minimization-what-actually-transits-slack--discord)).

## Top-level

```toml
idle_threshold_seconds = 60
gating = "idle_or_background"   # idle_only | background_only | idle_or_background | always
tool_input_max_chars = 400      # truncation cap for tool-input code blocks
```

| Key | Default | Notes |
| --- | --- | --- |
| `idle_threshold_seconds` | `60` | Seconds of macOS idle (HID) before a ping is allowed. Ignored on non-macOS. |
| `gating` | `"idle_or_background"` | When to send. `idle_only` requires the keyboard to have been idle past the threshold. `background_only` requires the agent's terminal to be unfocused. `idle_or_background` (default) requires either. `always` sends every time. |
| `tool_input_max_chars` | `400` | Cap on the inline `tool_input` code-fence section sent to Slack/Discord. Larger inputs are head-truncated with an `…` marker. |

Gating fails *open* on non-macOS or on `ioreg` / `osascript` failure —
you still get pinged rather than silently dropping events.

## `[events.<kind>]`

```toml
[events.permission]
enabled = true
gating = "always"               # approvals are urgent — always ping

[events.idle_prompt]
enabled = true

[events.elicitation]
enabled = true

[events.turn_complete]
enabled = true
```

Each event kind can be disabled or have its own `gating` override.
Permission requests default to `always` because a blocked tool call
needs your eyes regardless of whether you stepped away.

## `[display]`

Controls the *shape* of what reaches the destination.

```toml
[display]
verbosity = "terse"             # terse | normal | minimal
coalesce_window_seconds = 2.5
message_max_chars = 0
message_preview_head_chars = 250
message_preview_tail_chars = 250
```

| Key | Default | Notes |
| --- | --- | --- |
| `verbosity` | `"terse"` | `terse` = compact layout, 1-line iOS preview, full body. `normal` = explicit Project / Session / Tool / App field block. `minimal` = title only — no tool name, tool input, message body, transcript snippet, cwd, session id, or source app ever leaves the machine. Use `minimal` in compliance-sensitive environments. |
| `coalesce_window_seconds` | `2.5` | Hold a `turn_complete` ping this long so a follow-up `idle_prompt` can suppress it (you get one "waiting on you" rather than "finished" + "waiting on you" back-to-back). Set to `0` to disable. |
| `message_max_chars` | `0` | `0` = full text. Slack/Discord clients auto-collapse long messages and we split across blocks/embeds when over platform limits (Slack 3000/section, Discord 4096/embed). Set a positive int to hard-cap. |
| `message_preview_head_chars` | `250` | Slack-bot only. When a body exceeds `head + tail + 5`, post a head…tail elision with a **Show more** button that rewrites the message in place to the full body. Set to `0` (both) to disable. Ignored when `verbosity = "minimal"` or `message_max_chars > 0`. |
| `message_preview_tail_chars` | `250` | See above. |

## `[summary]`

Local extraction of the agent's last assistant message for
`turn_complete` events. Purely local — no API keys, no network.

```toml
[summary]
enabled = true
head_chars = 2000
tail_chars = 2000
```

Sized larger than `display.message_preview_*chars` so the **Show more
/ Show less** toggle has room to elide and expand. Set both small
(e.g. 250/250) for aggressive pre-truncation; set both very large to
defer all UX truncation to the toggle.

## `[slack.workspaces.<name>]`

Define one or more Slack workspaces. The wizard `agent-notify slack
add` writes a block for you and stores tokens in macOS Keychain. You
can have multiple workspaces and select between them with `[[routes]]`.

```toml
[slack.workspaces.default]
enabled = true

# --- credentials (pick ONE source per token; see SECURITY.md) ---
bot_token_keychain = "default:bot_token"     # xoxb-…  (recommended on macOS)
app_token_keychain = "default:app_token"     # xapp-…  (only for actionable_approvals)
# bot_token_env    = "SLACK_BOT_TOKEN"
# bot_token        = "xoxb-…"               # discouraged; warns on loose perms
# webhook_url      = "https://hooks.slack.com/services/…"  # webhook-only flow

# --- routing & UX ---
channel = "@me"                              # DM with the bot (secure default)
interactive = true                           # approve/deny buttons on pings
actionable_approvals = true                  # block PermissionRequest, inject decision

# --- approver gating (required for any non-DM channel) ---
approver_user_ids = ["U0YOURID"]
approver_user_groups = ["S01OPSTEAM"]        # optional Slack usergroup

# --- timing ---
approval_timeout_seconds = 300               # 0 = wait forever
```

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | Master on/off for the workspace. |
| `webhook_url` | — | Incoming-webhook URL. Webhook flow has no buttons / no approvals / no Show more toggle. |
| `bot_token` / `bot_token_env` / `bot_token_keychain` | — | `xoxb-…` token. Required for `interactive` and `actionable_approvals`. |
| `app_token` / `app_token_env` / `app_token_keychain` | — | `xapp-…` token (Socket Mode). Required only when `actionable_approvals = true`. |
| `channel` | `"@me"` | Where notifications post. `@me` DMs the installing user. Channel ID (`C…`), `#name`, or `@user` work too. |
| `interactive` | `false` | Adds Approve/Deny buttons to non-blocking pings. Requires `bot_token`. |
| `actionable_approvals` | `false` | Makes the `PermissionRequest` hook block until you tap a button (or the timeout fires). Requires `bot_token`, `app_token`, and a running daemon. |
| `approver_user_ids` | `()` | Allowlist of Slack `user_id`s permitted to click Approve/Deny. |
| `approver_user_groups` | `()` | Allowlist of Slack usergroup IDs. Members are checked at click time. |
| `approval_timeout_seconds` | `300.0` | After this many seconds the hook fails closed (deny). Set to `0` to wait forever — the approval stays pending until you resolve it from any device, no auto-deny. Note: Claude Code's own hook timeout in `~/.claude/settings.json` also caps how long the subprocess can wait. |

**Empty-allowlist rule.** When `actionable_approvals = true` and both
`approver_user_ids` / `approver_user_groups` are empty, config-load
*requires* `channel = "@me"` (DM with the bot). Anything else aborts
with a `ConfigError`. The daemon also runtime-checks that incoming
clicks came from a DM (channel ID starts with `D`) before accepting
them, so a stray post to a shared channel can never rubber-stamp tool
calls. See [SECURITY.md](SECURITY.md#network-attack-surface) for
context.

## `[sinks.discord]`

```toml
[sinks.discord]
enabled = false
webhook_url = "https://discord.com/api/webhooks/…"
```

Webhook only — no Discord-side button support. `display.verbosity` is
honored. Markdown renders natively (Discord accepts CommonMark).

## `[[routes]]` — per-repo routing

Need different projects pinging different Slack workspaces or channels?
Add `[[routes]]` blocks. The first whose `cwd` glob matches the hook's
working directory wins; overrides merge on top of the selected workspace.

```toml
# Two workspaces defined up top via `agent-notify slack add --name <name>`
# [slack.workspaces.home] and [slack.workspaces.work] — tokens in Keychain.

# Work repos route to the `work` workspace + a shared channel.
[[routes]]
cwd = "~/work/acme-*"
slack.workspace = "work"
slack.channel = "#agents-acme"

# OSS stays on the personal workspace.
[[routes]]
cwd = "~/oss/my-library"
slack.workspace = "home"

# Personal stuff stays quiet.
[[routes]]
cwd = "~/personal/*"
slack.enabled = false
```

`slack.workspace = "<name>"` picks the base config; any other `slack.*`
key patches a single field on top. Without `slack.workspace`, the route
uses the `default` workspace.

Patterns use `fnmatch`-style globs (`*`, `?`, `[abc]`) with `~`
expansion. Run `agent-notify doctor` from inside a repo to confirm
which route (if any) matches the current `cwd`.

### Strict routing — no cross-project leakage

**As soon as any `[[routes]]` entry exists, routing becomes strict:** if
the hook's `cwd` doesn't match *any* route, the notification is
silently skipped (with a one-line stderr note) instead of falling back
to the default workspace. This prevents the hazard where you clone a
new repo you forgot to route, and its events accidentally ping the
channel of a different project.

If you *want* a catch-all, add one explicitly at the end:

```toml
[[routes]]
cwd = "~/work/acme-*"
slack.webhook_url = "https://hooks.slack.com/services/work/…"

[[routes]]
cwd = "*"                                              # catch-all
slack.webhook_url = "https://hooks.slack.com/services/personal/…"
```

## Phone-tap approvals — visual reference

When `actionable_approvals = true` is on and the daemon is running, the
permission DM that lands on your phone is interactive — not just a
notification.

**Sidebar color** reflects what the message is asking of you:

| Tier   | Color  | When |
| ------ | ------ | ---- |
| Green  | `#2eb67d` | Informational / done / question (turn_complete, idle_prompt, AskUserQuestion) |
| Yellow | `#ecb22e` | Action required (permission requests on real tool calls) |
| Red    | `#a30f18` | Danger override — `tool_input` matched a destructive Bash pattern |

**Button kinds**, picked automatically from the tool:

- **Approve / Deny** — the default for any tool needing permission.
  Approve has a confirmation dialog (an accidental lock-screen tap
  shouldn't run a Bash command); Deny is one-tap. Deny-with-reason
  opens a modal so you can drop a one-liner that gets piped back to
  the agent.
- **Option buttons** — when the tool is `AskUserQuestion`, one button
  per option label. The first option whose label contains
  `(Recommended)` gets a filled green CTA. Multi-question questions
  render as separate actions blocks; tapping records a partial answer
  (the message updates with a ✓ on each answered question), and the
  approval finalizes only when every question has an entry. The hook
  then pre-fills the answers via `decision.updatedInput`, so the
  AskUserQuestion tool returns immediately without prompting in the
  terminal.
- **Suggestion buttons** — when Claude Code attaches
  `permission_suggestions` to the request (e.g. *"add `Bash(curl:*)` to
  localSettings"*), each one becomes an extra button below
  Approve/Deny. Tapping resolves the approval as `allow` and emits
  `decision.updatedPermissions` so the rule edit is applied
  immediately — extending your allowlist in one tap.
- **Show more / Show less** — long bodies (e.g. ExitPlanMode plans,
  long turn-complete summaries) post as a head…tail elision with a
  Show more button that rewrites the message in place. Toggle clicks
  hit the same approver-allowlist gate.

**Resolved-message wording** updates in place via `chat.update`:

| Outcome | Header |
| --- | --- |
| Plain Approve | ✅ Approved by @you |
| Plain Deny | 🚫 Denied by @you |
| Single-question option click | ✅ Selected `<label>` by @you |
| Multi-question all-questions answered | ✅ Answered by @you (+ Q→A summary block) |
| Suggestion click | ✅ Approved & applied `<rule>` by @you |
| Timeout / failed Slack post | ⏳ Timed out — denied (or stays pending if `approval_timeout_seconds = 0`) |

## Verification

`agent-notify doctor` summarizes everything and probes:

- Config file presence and parse status.
- Per-workspace `auth.test` against Slack's API.
- Approver-allowlist size per workspace; flags the documented DM-only
  fallback when `channel = "@me"` and the allowlist is empty.
- launchd plist + load status when `actionable_approvals = true`
  (warns about stale legacy plists from older installs).
- Whether the current `cwd` matches a configured route.
- Idle / frontmost state.
