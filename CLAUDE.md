# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repo.

## Project in one line
A Python CLI (`agent-notify`) that forwards "agent needs attention" events from Claude Code and Codex to Slack / Discord, gated by macOS idle + frontmost-app so you don't get spammed while actively at the keyboard.

## Toolchain
- **uv** manages venv + deps. Always `uv run <cmd>` rather than activating a venv or calling `python` directly.
- **Python 3.11+** (`tomllib` is stdlib; keep it that way — don't add `tomli`).
- **tomlkit** is the only runtime dependency. Reserve it for round-tripping user-owned config files (`~/.codex/config.toml`) where preserving unrelated keys matters. For our own config, stdlib `tomllib` is fine.
- **pytest + pytest-cov**, 80% line coverage gate enforced in `pyproject.toml` (`--cov-fail-under=80`). Don't drop the gate to dodge a failure — add the test.

## Layout
```
src/coding_agent_notifier/
  cli.py           # argparse entry point; subcommands: hook, config, install, test, doctor
  config.py        # TOML → frozen Config dataclass; ConfigError on bad input
  event.py         # Normalized Event dataclass (shared by sources + sinks)
  gating.py        # Pure should_send(event, config, state) decision
  macos.py         # ioreg HIDIdleTime + osascript frontmost; None-returning on non-macOS
  install.py       # Idempotent merges into ~/.claude/settings.json, ~/.codex/{config.toml,hooks.json}
  sources/         # {claude_code,codex}.parse(payload) -> Event | None
  sinks/           # SlackSink, DiscordSink, shared http_post_json helper
tests/             # Mirrors package layout; fixtures/ holds real-shape agent payloads
```

## Non-obvious conventions
- **Hooks must never block the agent.** `cli.main` catches every `Exception` and returns 0. Sink errors are logged to stderr, not raised. Preserve this — a failing webhook must not stop Claude Code.
- **Gating fails open.** If `idle_seconds()` or `frontmost_app()` return `None` (non-macOS, command missing, parse failure), we send the notification rather than swallow it. Tests assert this explicitly — don't "fix" it.
- **Sources return `Event | None`.** Returning `None` means "known event, intentionally not routed" (e.g. `Notification:auth_success`). The CLI treats `None` as a silent no-op. Don't raise.
- **`source_app` comes from `$TERM_PROGRAM`** at hook fire time, translated via `macos.TERM_PROGRAM_TO_APP`. Add new mappings here when adopting new terminals.
- **Installers back up the target** to `<path>.bak-<timestamp>` before writing. Keep them idempotent: re-running must be a no-op.
- **HTTP goes through `sinks.base.http_post_json`** (stdlib `urllib`). Don't add `requests` / `httpx` just for one more call — keep the dependency surface small.
- **Actionable approvals use `PermissionRequest`, not `PreToolUse`.** PermissionRequest fires only when Claude Code was about to prompt the user (auto-allowed tools skip it), so the matcher stays `*` without spamming. Output schema is `decision.behavior` (allow/deny only — no "ask"/"defer"); reasons go in `decision.message`. `updatedInput` pre-fills tool params (used for AskUserQuestion answers); `updatedPermissions` applies a rule edit (used for permission_suggestions). The blocking flow only kicks in when the resolved workspace has `actionable_approvals = true` — otherwise we fall through to the normal parse-and-send notification path.
- **Per-button click decoding.** Action IDs are parsed in `slack_socket.handle_block_actions`: `agent_notify_approve`/`agent_notify_deny` for the standard pair, `agent_notify_option_<q>_<o>` for AskUserQuestion option clicks (legacy single-Q `_<o>` is also accepted for back-compat), and `agent_notify_suggestion_<i>` for suggestion clicks. Don't change these strings without bumping the back-compat matrix.

## Testing patterns
- Hook payloads live in `tests/fixtures/*.json` as real-shape examples. Add a fixture when supporting a new event type; load via the `load_fixture` conftest fixture.
- HTTP is patched by swapping `coding_agent_notifier.sinks.<sink>.http_post_json` with a fake — see `tests/test_sinks.py::_FakePoster`. Don't monkey-patch `urllib` directly.
- macOS helpers are patched by replacing `macos.subprocess.run` and `macos.is_macos` — see `tests/test_macos.py`.
- `cli.py` uses `coding_agent_notifier.cli.macos.<fn>` indirection on purpose so CLI tests can swap idle/frontmost without touching `macos` globally.

## Commands you'll run
```bash
uv sync                                       # install deps
uv run pytest                                 # full suite + coverage gate
uv run pytest tests/test_gating.py -x         # focused
uv run agent-notify --help                    # CLI surface
echo '{"hook_event_name":"Stop","cwd":"/tmp","session_id":"demo"}' \
  | uv run agent-notify hook --source claude-code
```

## Smoke testing changes against the live daemon
`uv run pytest` validates code correctness, but the binary on PATH and the
running daemon are still the *previous* build until you reinstall + restart.
For any change that touches code reachable by the hook subprocess or the
Socket Mode daemon (sinks, config parsing, sources, `slack_socket.py`,
`cli.py`), finish the loop with:

```bash
uv tool install --reinstall '.[slack-bot]'                              # reinstall the user-tool binary (non-editable — required)
launchctl kickstart -k "gui/$(id -u)/app.coding-agent-notifier.daemon"  # SIGTERM + relaunch the daemon
launchctl list | grep coding-agent-notifier                             # verify a fresh PID is present
```

Notes:
- The reinstall is **not editable** — `uv tool install` is a one-shot copy
  into `~/.local/share/uv/tools/`. Without `--reinstall`, uv treats the
  existing install as up-to-date and skips your changes silently.
- `kickstart -k` sends SIGTERM and relaunches under launchd. The exit
  status column in `launchctl list` will read `-15` after the restart —
  that's the prior process's signal, not a failure of the new one.
- Pure test-only or docs-only changes don't need this step.
- If the daemon isn't a launchd service on this machine (e.g. running
  in a foreground terminal via `agent-notify slack run`), restart it
  the same way you launched it.

## When adding a new sink
1. New file in `sinks/`, dataclass wrapping its config, `send(event)` method.
2. Corresponding config block in `config.py` with a `ConfigError` when enabled-but-unconfigured.
3. Wire into `cli._dispatch`.
4. Mirror the existing test shape in `tests/test_sinks.py` (fake HTTP + disabled/missing-config cases).

## When adding a new event kind
1. Add to `EventKind` literal + `KIND_TITLES` / `KIND_EMOJI` in `event.py`.
2. Add to `VALID_EVENT_KINDS` in `config.py`.
3. Update the source parsers that produce it.
4. Add a fixture + source test.
