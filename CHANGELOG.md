# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-04-25

First public release.

### Added

- `agent-notify` CLI for forwarding "agent needs attention" events from
  Claude Code (`Notification`, `Stop`, `PermissionRequest`,
  `UserPromptSubmit` hooks) and Codex (legacy `notify` and
  `hooks.json` shapes) to Slack and/or Discord.
- macOS gating: notifications only fire when the user is idle or the
  agent app isn't frontmost (configurable via `gating` and
  `idle_threshold_seconds`). Fails open on non-macOS so Linux users still
  get pings.
- Multi-workspace Slack support via `[slack.workspaces.<name>]` blocks.
  Per-repo routing via `[[routes]]` selects a workspace by name and
  patches individual fields on top.
- **Phone-tap approvals** (Slack only): when a workspace sets
  `actionable_approvals = true`, `PermissionRequest` blocks until you
  click an approve / deny button on your phone. Supports
  `permission_suggestions` rule edits, AskUserQuestion option pickers,
  multi-question flows, custom freeform answers via modal, and
  deny-with-reason via modal. Fails closed on any error path.
- Slack Socket Mode daemon (`agent-notify daemon`) supervised by launchd
  with `KeepAlive=true` and `ThrottleInterval=10` to bound respawn rate.
- Setup wizard `agent-notify slack add` that stores tokens in macOS
  Keychain, verifies via `auth.test`, and writes the workspace block.
- Token-format soft-warnings in the wizard for the common `xoxb-`/`xapp-`
  swap typo.
- `agent-notify doctor` now lists configured workspaces with live
  `auth.test` results, checks the launchd daemon when any workspace
  enables `actionable_approvals`, and warns loudly when the current
  `cwd` matches no `[[routes]]` entry.
- Rotating-file logging for the daemon (`~/.agent-notify/logs/daemon.log`,
  10 MiB × 3 backups) and a global `--debug` flag to lower the
  stderr log level on demand.
- One-time auto-migration from legacy XDG state
  (`~/.config/coding-agent-notifier`, `~/.cache/coding-agent-notifier`)
  to the new dot directory `~/.agent-notify/`.
- `SECURITY.md` documenting the threat model, secret-handling tradeoffs,
  and reporting process.
- **Show more / Show less toggle** for long Slack message bodies
  (turn-complete + ExitPlanMode plan approvals). When the rendered body
  exceeds `display.message_preview_head_chars` + `_tail_chars` + 5
  (defaults 250 + 250), the message posts as a head…tail elision with a
  "Show more" button; tapping rewrites the message in place to the full
  body and a "Show less" button. Persisted via a new
  `expandable_messages` state directory. Skipped when `verbosity =
  "minimal"`, when `display.message_max_chars > 0`, or when only a
  webhook is configured (no `chat.update`).
- **Markdown → Slack mrkdwn converter** (`sinks/mrkdwn.py`) so agent
  messages render bold/italic/strike/links/headings/bullets natively
  instead of showing literal `**bold**` / `## Heading` characters.
  Fenced + inline code regions are protected (markdown inside ``` stays
  literal). Discord is untouched — it accepts CommonMark natively.
- **Markdown table rendering**: 1-/2-/N-column markdown tables in agent
  output convert to bullet-per-row blocks (`• *col:* val`). Cell content
  still flows through bold/italic/link/inline-code conversion.
- **Top-of-message preview text polish**: the Slack `text` field (iOS
  push preview + the line above the green attachment) now runs through
  `_mrkdwn_polish` so bold/italic/strike/links render in the preview
  instead of literal markdown markers.
- **ExitPlanMode plan-approval improvements**: plan body no longer
  wrapped in a code fence (renders as formatted markdown); approval
  message gets the Show more / Show less toggle alongside Approve / Deny
  / Deny-with-reason buttons; heading regex matches all heading levels
  so `## Summary` becomes the title `Summary`.
- **`approval_timeout_seconds = 0` means wait forever** — leave the
  approval pending until resolved from Slack on any device (no
  auto-deny). Earlier behavior of timing out after N seconds is still
  the default (300s).
- New `[display]` keys: `message_max_chars`, `message_preview_head_chars`,
  `message_preview_tail_chars` for controlling body truncation and the
  Show more / Show less budget.
- `[summary] head_chars` / `tail_chars` defaults bumped 250 → 2000 so
  turn-complete bodies have room for the toggle to elide.
- `agent-notify doctor` reports the approver-allowlist size per
  workspace (`approvers: N user(s), M group(s)`); annotates the
  documented DM-only fallback when the allowlist is empty and
  `channel = "@me"`.
- README gains a "Network attack surface" subsection in Security
  spelling out the outbound-only design (no listener, no localhost
  port, click-path auth-gated, no `shell=True`).

### Security

- Trust boundary documented: local user account; we do not defend against
  a compromised harness or local root. See `SECURITY.md` for details.
- `permission_suggestions` payloads from the hook are validated
  structurally before they can reach `decision.updatedPermissions`;
  malformed entries are dropped and logged.
- State files (`config.toml`, `secrets.toml`, FIFOs, approval records)
  are written through a `0600`-enforcing helper that ignores umask.
- `secrets.toml` permissions are hard-enforced at `0600` (loose perms
  abort the load); `config.toml` warns on loose perms when inline
  secrets are present.
