# Proposal: interactive Slack bot — approve/deny in the message itself

> Status: proposal / spec. No code in this pass. Companion to
> `ios-live-activities.md` — same blocking-hook machinery, different UX
> surface. Both can ship independently; Slack ships first (far lower lift).

## 1. Context & goals

Today `agent-notify` talks to Slack via an **incoming webhook** — a fire-and-
forget POST that can render a message but can't receive interaction. So when
a `permission` event fires, the user sees the ping but still has to switch to
the terminal to approve or deny.

The [iOS proposal](./ios-live-activities.md) solves this with lock-screen
action buttons that unblock a `PreToolUse` hook on the Mac. That architecture
is general: the hook-blocking + `PendingApprovals` machinery is UI-agnostic.
The iOS sink is just one client of it.

A **Slack bot** can be a second client — approve/deny buttons rendered inside
the Slack message, driving the same back-channel. For most users this is the
better first implementation: no Apple Developer account, no iOS code, no
APNs, no physical device required.

**Goals:**

- Approve/deny buttons inline in the Slack message for `permission` events.
- Show full tool details (long `tool_input` that wouldn't fit in a message)
  in a Slack modal on demand.
- In-place message edits that show the outcome: "Approved by @chris at 10:42"
  / "Denied" / "Timed out → denied". No ambiguity about what happened.
- Run with **no public server infrastructure**. No ngrok, no Cloudflare
  Worker, no port forward.
- Coexist with the existing webhook sink so users who don't want to install
  a Slack App keep working.

**Non-goals:**

- Replacing the iOS proposal. They target different trust models — Slack is
  "fast path, Slack-trusted"; iOS is "slow path, zero-trust". Both have a
  place.
- Payload privacy from Slack. Slack is the TCB here; anything sent is
  readable by Slack. Users who need E2EE want the iOS path.
- Public Slack App distribution to other workspaces (that's a v2 concern —
  v1 is private/workspace-local install).

## 2. High-level architecture

```
Claude Code / Codex
    │ hook
    ▼
agent-notify  (Mac; existing)
    ├── SlackBotSink  ─── chat.postMessage ─── Slack ─── user sees buttons
    │                                            │
    │                                   Socket Mode WS
    │                                            │
    └── PendingApprovals (shared with iOS plan)  │
         ▲                                        │
         │ unblocks                               │ button click
         └── PreToolUse hook (blocks here) ◄──────┘
```

Two key observations:

1. **Slack Socket Mode removes the public-URL requirement.** Slack's standard
   interactivity model expects a public HTTPS endpoint it can POST to. Socket
   Mode flips it: our Mac opens an outbound WebSocket to Slack, Slack
   delivers button clicks / slash commands / modal submissions over the WS.
   No open ports, no tunnel. Perfect fit for a local-first notifier.
   Canonical: <https://api.slack.com/apis/socket-mode>.
2. **`PendingApprovals` and the blocking `PreToolUse` hook are reused verbatim.**
   They are the shared infrastructure between the iOS and Slack proposals.
   The Slack bot is a smaller lift precisely because it's the second client
   of machinery the iOS plan already specifies. See
   [ios-live-activities.md §3d and §6](./ios-live-activities.md#3d-pendingapprovals-registry-mac-side-new-module).

## 3. Components

### 3a. Slack App (workspace-local, declarative manifest)

Declared via Slack's [App Manifest](https://api.slack.com/reference/manifests)
as a single YAML file checked into the repo under `docs/slack-app-manifest.yaml`.
Users install by going to `https://api.slack.com/apps` → *Create New App* →
*From a manifest*, pasting the YAML. One-click install to their workspace.

Scopes needed (bot token):

- `chat:write` — post messages.
- `chat:write.public` — post in channels the bot isn't explicitly invited to
  (optional; defaults off, user opts in).
- `im:write` — DM the user directly (preferred over channel posts).
- `im:history` — needed only if we add threaded follow-ups in v2.
- `commands` — slash commands (`/agent-notify status`, etc.). Optional v1.

App-level features enabled:

- **Socket Mode** — generates an `xapp-*` app-level token. The manifest
  declares Socket Mode enabled; user generates the token post-install.
- **Interactivity & Shortcuts** — enabled, but *no Request URL* (Socket Mode
  mode). Just declares we want interactivity events delivered.

### 3b. `SlackBotSink` (Mac-side, new sink)

Replacement for `SlackSink` when `[slack_bot]` is configured. Both can
coexist; config chooses one or both, per the same additive pattern as
`DiscordSink`.

- Uses the bot token (`xoxb-*`) via `chat.postMessage` to post rich Block Kit
  messages.
- Builds Block Kit output using the same `tool_formatters.py` machinery as
  today, just with an extra `actions` block containing approve/deny buttons
  for `permission` events.
- Button payload carries the approval `event_id` so Slack echoes it back on
  click.
- Message's `ts` (Slack message timestamp) is stored in `PendingApprovals`
  so we can edit the message in place via `chat.update` when the decision
  arrives.

### 3c. Socket Mode client (Mac-side, new module)

New file: `src/coding_agent_notifier/slack_socket.py`.

- Opens WebSocket to Slack using the app-level token (`xapp-*`).
- Runs in its own daemon thread (or asyncio task) inside the long-lived
  `agent-notify` process. Because today `agent-notify` is a per-hook
  short-lived CLI, **we add a small always-on helper daemon** — see §3e —
  that owns this connection. Hook invocations themselves stay short.
- Handles three event types:
  - `block_actions` — approve/deny button click → resolve `PendingApproval`.
  - `view_submission` — modal submission (used only if we add a "provide
    feedback" modal for deny actions).
  - `slash_commands` — `/agent-notify status` etc. Optional v1.
- On button click: calls `PendingApprovals.resolve(event_id, decision, actor=user_id)`,
  then `chat.update`s the original message in place to show outcome.

### 3d. User authorization

Not every workspace member should be able to approve tool calls for the
user's agent. Config declares an explicit allowlist:

```toml
[slack_bot]
approver_user_ids = ["U0123ABC", "U0456DEF"]   # Slack user IDs
```

On `block_actions`, the Socket Mode handler rejects any button click from a
user not in `approver_user_ids` — replies ephemerally "Not authorized" and
does not resolve the approval. If the config omits `approver_user_ids` and
the bot is set to DM-only mode, we implicitly allow only the DM recipient
(the user who installed the bot).

### 3e. Always-on helper daemon

This is the structural difference from the iOS plan. iOS events are driven by
pushes the Mac *sends*; Slack button clicks come *in*, which means something
on the Mac must be always-listening.

Design:

- A new command: `agent-notify daemon` — blocks, maintains the Socket Mode
  connection, handles inbound events. Reconnects with exponential backoff on
  disconnect. Stderr → `$XDG_CACHE_HOME/coding-agent-notifier/daemon.log`.
- Launched via `launchd` (macOS) using a launch agent plist we ship; installed
  by `agent-notify install slack-bot`. Stays up across reboots.
- Hook processes stay short-lived and do NOT open their own WS. They just
  write `PendingApprovals` entries; the daemon picks up button clicks and
  resolves them.
- IPC between hooks and daemon is already solved by `PendingApprovals`
  (the fcntl-locked JSON registry from the iOS plan). The daemon polls /
  watches the registry for new entries to push to Slack, OR — simpler — the
  hook itself calls `chat.postMessage` directly over HTTPS (no daemon
  needed for the outbound path) and the daemon only handles inbound WS.

The "daemon handles inbound only" split is the simplest shape and what this
doc recommends:

- **Outbound** (hook → Slack message) happens inside the short-lived hook
  process. No daemon involvement.
- **Inbound** (Slack button → hook unblock) goes through the daemon, which
  writes to `PendingApprovals` and wakes the blocked `PreToolUse` hook via
  the same FIFO mechanism specified in
  [ios-live-activities.md §3d](./ios-live-activities.md#3d-pendingapprovals-registry-mac-side-new-module).

### 3f. Reuse from the iOS proposal

Reads of [ios-live-activities.md](./ios-live-activities.md) supply:

- `PendingApprovals` registry shape (§3d there).
- `PreToolUse` blocking hook wire-up (§6 there).
- Fail-closed timeout policy (§6 there).

This proposal doesn't re-specify them — ship them once, use them from both
sinks.

## 4. Slack App manifest (sketch)

The full manifest lives at `docs/slack-app-manifest.yaml` alongside this
proposal when it lands. Shape:

```yaml
display_information:
  name: agent-notify
  description: Actionable agent notifications for Claude Code / Codex
  background_color: "#1a1a1a"
features:
  bot_user:
    display_name: agent-notify
    always_online: true
  slash_commands: []      # v1: none; add later
oauth_config:
  scopes:
    bot:
      - chat:write
      - im:write
settings:
  event_subscriptions:
    bot_events: []        # Socket Mode carries these
  interactivity:
    is_enabled: true      # no request_url — Socket Mode
  socket_mode_enabled: true
  token_rotation_enabled: false   # v1 simplicity
```

No "Request URL" anywhere — Socket Mode carries interactivity. That's the
bit that makes this ship-able without any public server.

## 5. UX

### Permission event (gets buttons)

Message shape (Block Kit):

```
:warning:  *Claude Code needs approval*
iburnapp.github.io · sess b8d1 · Terminal

*Bash:* `rm -rf ./dist`

[ Approve ]   [ Deny ]   [ View details ]
```

- "Approve" button style `primary`.
- "Deny" button style `danger`.
- "View details" opens a modal (`views.open`) with the full `tool_input`
  rendered as code — useful when the Bash command or file diff is too long
  for the message body.
- All three buttons carry `value = event_id`, so Slack echoes the approval
  identity back on click.

After click, `chat.update` rewrites the message in place:

```
:white_check_mark:  *Approved by @chris · 10:42:03*
iburnapp.github.io · sess b8d1

*Bash:* `rm -rf ./dist`
```

Buttons are removed. Clear audit trail in-channel.

### Timeout

If the `PreToolUse` hook hits its configured timeout before any click
arrives, the daemon (or the hook on exit) edits the message:

```
:hourglass:  *Timed out after 10 min → denied*
```

Fail-closed, exactly as in the iOS plan.

### Other event kinds

`idle_prompt`, `turn_complete`, `elicitation` stay as one-way messages
matching today's output — no buttons. Only `permission` gets the interactive
surface in v1. (`turn_complete` with a "Show transcript" button that opens
a modal is a tempting v2; punt.)

### DM vs channel

Default: DM the installing user. Cleaner audit trail, simpler allowlist, no
risk of a coworker seeing sensitive `tool_input`. Channel mode is supported
via explicit config but is documented as "opt-in, allowlist required".

## 6. Hook wiring

Same `PreToolUse` blocking contract as the iOS plan. See
[ios-live-activities.md §6](./ios-live-activities.md#6-hook-wiring-mac-side).

Delta from that doc: the `[slack_bot] actionable_approvals = true` flag is
what turns the hook on when Slack is the sink. If both iOS and Slack have
`actionable_approvals = true`, the hook sends to both — whichever resolves
first wins; the other sink gets its message edited to "Resolved elsewhere".
That's a small but real feature, and it costs little once `PendingApprovals`
is the shared ledger.

## 7. Config surface

```toml
[slack_bot]
enabled = true
bot_token_env = "SLACK_BOT_TOKEN"         # xoxb-*, in env not config
app_token_env = "SLACK_APP_TOKEN"         # xapp-*, in env not config
default_channel = "D01ABCDEF"             # DM ID preferred; or "#agent-notify"
approver_user_ids = ["U0123ABC"]          # allowlist for button clicks
actionable_approvals = true               # wire the blocking PreToolUse hook
```

Parser invariants (validated in `config.py`):

- Tokens via env only, never in TOML — matches the existing webhook-URL
  pattern.
- `actionable_approvals = true` requires `app_token_env` set (Socket Mode
  needs the app-level token).
- `approver_user_ids` required when `default_channel` is a public channel;
  optional but strongly recommended when DM-only.

## 8. Security / threat model

| # | Threat                                  | Mitigation                                                                       | Residual                                                            |
| - | --------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| 1 | Slack reads all payloads                | Out of scope — Slack is the TCB in this proposal. Users wanting E2EE → iOS path. | Everything sent is in Slack's hands.                                |
| 2 | Coworker in same workspace spoofs approve | `approver_user_ids` allowlist; DM-only by default.                               | Admin in workspace can impersonate; acceptable for single-user tool. |
| 3 | Bot token leak                          | Token via env, not TOML; rotate via Slack UI; scoped to `chat:write` + `im:write`. | Attacker can spam user's DM until rotated.                          |
| 4 | App-level token leak                    | Env only; rotate. Scoped to Socket Mode (receives events — can't mint messages). | Attacker can see inbound events but not approve (needs user ID + session). |
| 5 | Socket Mode connection drop             | Exponential backoff reconnect; in-flight approvals fall back to hook `timeout` → deny. | During disconnect window, all approvals fail-closed.                |
| 6 | Slack workspace admin compromise        | Nothing we can do — workspace admin has full access.                             | Accept; document in README.                                         |
| 7 | Message-body metadata leak              | `tool_input` redaction policy (from CLAUDE.md) applies equally to Slack posts.    | Redaction is best-effort.                                           |

Notes:

- Row 2 is the most common real-world concern for a workspace install.
  Default to DM mode in the `agent-notify install slack-bot` flow, and print
  a big yellow notice if the user explicitly switches to a channel.
- Rows 3/4: tokens in env, not config, matches the existing pattern and
  keeps `~/.config/coding-agent-notifier/config.toml` safe to commit as a
  dotfile example.
- Row 5 is the analog of iOS's "Tailscale down" — fail-closed ensures we
  never silently approve.

No new crypto. Slack is a fully trusted third party; the Ed25519 /
XChaCha20-Poly1305 machinery from the iOS doc is not needed here.

## 9. Install flow

The `agent-notify install slack-bot` command does four things:

1. Prints instructions to create the Slack App from the manifest
   (`docs/slack-app-manifest.yaml`), install to workspace, generate the app-
   level token.
2. Prompts for `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`, writes them to a
   user-chosen secure location (`pass`, macOS Keychain, or a `.env` file
   with 0o600) and adds the corresponding `*_env` to config.
3. Writes a `launchd` plist at `~/Library/LaunchAgents/com.chrisballinger.agent-notify-daemon.plist`
   that runs `agent-notify daemon`. Loads it via `launchctl load`.
4. Merges the same Claude Code hook entries the existing
   `install claude-code` path merges, plus the new `PreToolUse` entry if
   `actionable_approvals = true`.

All four steps are idempotent — matches the existing installer pattern from
`src/coding_agent_notifier/install.py`.

## 10. Prototyping order

Smaller ladder than the iOS plan because many pieces are reused:

1. **`SlackBotSink` (outbound only).** Swap webhook → `chat.postMessage`.
   Same visual output as today, just posted through the bot. No buttons yet.
   Ships a nicer foundation even with zero interactivity.
2. **`agent-notify daemon` command + launchd plist.** Minimum daemon that
   opens the WS, logs every inbound event to `daemon.log`, does nothing
   else. Prove the always-on path works across reboots / network hops.
3. **Approve/deny buttons on `permission` messages.** Daemon resolves button
   clicks against `PendingApprovals`. `PreToolUse` hook still *not* blocking
   yet — buttons just observe and log. This lets us shake out Slack round-
   trip timing before the real gate depends on it.
4. **Blocking `PreToolUse` hook.** Now the button is load-bearing. Flip the
   config flag.
5. **Modal for "View details".** `views.open` with full `tool_input`.
6. **User-authorization allowlist + channel/DM mode polish.**
7. **Slash commands (`/agent-notify status`, etc.).** Optional.

If we never do step 7, the system is fine. If step 4 trips on something
unexpected in the hook protocol, steps 1–3 still ship a strictly better
Slack integration than today.

## 11. Comparison with the webhook sink and the iOS plan

| Axis                          | Webhook (today)                | Slack bot (this proposal)        | iOS (other proposal)             |
| ----------------------------- | ------------------------------ | -------------------------------- | -------------------------------- |
| Prereqs                       | Webhook URL                    | Slack App + tokens + launchd     | Apple Dev $99/yr, iOS device, Tailscale |
| Implementation lift           | Shipped                        | Moderate                         | Large                            |
| Public server required        | No                             | No (Socket Mode)                 | No (APNs is Apple's)             |
| Approve/deny from notification | No                             | Yes                              | Yes                              |
| Payload privacy from provider | No (Slack sees)                | No (Slack sees)                  | Yes (E2EE, NSE decrypts)         |
| Always-on daemon needed       | No                             | Yes (one)                        | No                               |
| TCB                           | Slack + Mac                    | Slack + Mac                      | Apple APNs (metadata) + Mac + iPhone |

Recommendation in the README once both ship:

- **Default for most users:** Slack bot if you live in Slack, iOS if you
  want real zero-trust.
- **Webhook:** stays as the zero-setup option for users who just want a ping.

## 12. Open questions

- **Single bot for multiple users?** v1 is single-user. Multi-user (shared
  `agent-notify` bot in a team Slack, each user has their own daemon
  pairing) is interesting but doubles the config complexity. Punt.
- **Thread mode?** Post approvals as a thread under a session root message
  to keep main channel clean. Probably a yes for v2; needs a
  `session_root_ts` column in `PendingApprovals`.
- **What about Discord?** Discord has interactions via a similar App
  mechanism (Interactions endpoint, or the `gateway` WS for slash commands
  and buttons). Same architecture would apply. Not in scope here; when we
  implement this, we factor the daemon + `PendingApprovals` wiring so a
  future `DiscordBotSink` is additive.
- **Audit log?** Same answer as the iOS proposal — append-only JSONL next
  to `defer.log`, `tool_input` redacted. Shared format across sinks.

## Out of scope for this doc

- Public Slack App distribution / Slack Marketplace submission.
- Discord bot counterpart.
- Multi-user / team install.
- Threading / session-grouped messages.
- Any iOS content (see `ios-live-activities.md`).
