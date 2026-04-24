# Plan: Distribute agent-notify as a Claude Code plugin

> Status: deferred. The notification UX improvements land first; this plan kicks
> in afterward. Archived from `~/.claude/plans/okay-i-want-to-zazzy-bentley.md`.

## Context

v0.1 of `agent-notify` is shipped and working end-to-end. Today, users install in two manual steps:

1. `uv tool install coding-agent-notifier` — puts the CLI on PATH.
2. `agent-notify install claude-code` — bespoke installer that merges JSON into `~/.claude/settings.json`.

Step 2 is replaceable with a Claude Code plugin so users get discoverability via the marketplace, one-command enable/disable via `/plugin`, and no hand-editing of personal settings. The Python CLI itself still has to be installed separately — Claude Code plugins are not designed to ship arbitrary Python packages — but that's an honest single-line prereq in the README.

**Goal:** publish this same repo as a Claude Code plugin (and a single-plugin marketplace):

```
uv tool install coding-agent-notifier
# inside Claude Code:
/plugin marketplace add chrisballinger/coding-agent-notifier
/plugin install agent-notify@coding-agent-notifier
```

## Design decisions

- **Monorepo, plugin nested in a subdirectory.** Plugin files live under `agent-notify/`. `.claude-plugin/marketplace.json` at the repo root registers the plugin; the plugin itself has its own `agent-notify/.claude-plugin/plugin.json`.
- **Plugin is a thin shim.** Declares hooks only; the Python CLI is the engine. `agent-notify/bin/agent-notify` is a bash shim that execs the user-installed `agent-notify`.
- **Self-hosting marketplace.** `.claude-plugin/marketplace.json` registers this repo as a single-plugin marketplace.
- **Keep `agent-notify install claude-code`** as an alternative for users who don't want plugins.
- **Hook command shape:** `${CLAUDE_PLUGIN_ROOT}/bin/agent-notify hook --source claude-code`.

## Files to add

```
coding-agent-notifier/
├── .claude-plugin/
│   └── marketplace.json
├── agent-notify/
│   ├── .claude-plugin/
│   │   └── plugin.json
│   ├── hooks/
│   │   └── hooks.json
│   └── bin/
│       └── agent-notify
└── src/ tests/ etc.
```

### `agent-notify/.claude-plugin/plugin.json`

```json
{
  "name": "agent-notify",
  "version": "0.1.0",
  "description": "Ping Slack or Discord when Claude Code needs your attention — only when you're away from the keyboard.",
  "author": {"name": "Chris Ballinger", "email": "chrisballinger@gmail.com"},
  "homepage": "https://github.com/chrisballinger/coding-agent-notifier",
  "repository": "https://github.com/chrisballinger/coding-agent-notifier",
  "license": "MIT",
  "keywords": ["notifications", "slack", "discord", "hooks", "claude-code"],
  "hooks": "./hooks/hooks.json"
}
```

### `.claude-plugin/marketplace.json`

```json
{
  "name": "coding-agent-notifier",
  "owner": {"name": "Chris Ballinger", "email": "chrisballinger@gmail.com"},
  "metadata": {"description": "Slack / Discord notifications for Claude Code."},
  "plugins": [{"name": "agent-notify", "source": "./agent-notify"}]
}
```

Schema confirmed: `name` + `owner` + `plugins[]` required; each plugin entry needs `name` + `source`. `source` must start with `./`. Version comes from the nested `plugin.json`.

### `agent-notify/hooks/hooks.json`

Mirrors the JSON that `install.py::CLAUDE_HOOK_ENTRIES` already generates, just rooted under `${CLAUDE_PLUGIN_ROOT}/bin/agent-notify`:

```json
{
  "hooks": {
    "Notification": [
      {"matcher": "permission_prompt|idle_prompt|elicitation_dialog",
       "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-notify hook --source claude-code"}]}
    ],
    "PermissionRequest": [
      {"matcher": "*",
       "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-notify hook --source claude-code"}]}
    ],
    "Stop": [
      {"matcher": "*",
       "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/bin/agent-notify hook --source claude-code"}]}
    ]
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` resolves to the plugin directory (`.../agent-notify/`), not the marketplace root.

### `agent-notify/bin/agent-notify` (shim, executable)

```bash
#!/usr/bin/env bash
# Delegate to the user-installed agent-notify on PATH. If not installed,
# print a one-line diagnostic and exit 0 so we never block Claude Code.
real=$(which -a agent-notify 2>/dev/null | grep -v "^$0\$" | head -n1)
if [ -n "$real" ]; then
  exec "$real" "$@"
fi
echo "agent-notify plugin: CLI not installed. Run: uv tool install coding-agent-notifier" >&2
exit 0
```

## Install flow (user-facing)

```bash
uv tool install coding-agent-notifier
# inside Claude Code
/plugin marketplace add chrisballinger/coding-agent-notifier
/plugin install agent-notify@coding-agent-notifier
agent-notify config init
$EDITOR "$(agent-notify config path)"
```

`coding-agent-notifier` in the install command is the **marketplace name** (from `marketplace.json#name`).

## `uvx` alternative (considered, rejected)

`command: "uvx --from coding-agent-notifier agent-notify hook ..."` would eliminate the `uv tool install` prereq but: (a) not documented in hook contexts, (b) `uvx` cold-start on every Stop is too much latency, (c) plugin should stay runtime-dep-free.

## Tests

- **`tests/test_plugin_manifest.py`** — load `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `agent-notify/hooks/hooks.json`; assert valid JSON + required fields + hook commands reference `agent-notify hook --source claude-code`.
- **`tests/test_shim.py`** — run `bash agent-notify/bin/agent-notify --version` with tmp_path-scoped fake `agent-notify` on PATH; assert exec. Then without the fake; assert stderr hint + exit 0.

## Verification

1. `uv run pytest` — manifest + shim tests green, coverage ≥ 80%.
2. `jq .` on the three JSON files — all parse.
3. `/plugin marketplace add chrisballinger/coding-agent-notifier` + `/plugin install agent-notify@coding-agent-notifier` — verify via `/plugin` that it shows enabled.
4. In a new terminal, run `claude`, trigger `Stop` → confirm Slack ping.
5. `/plugin uninstall agent-notify` — confirm hooks stop firing.
6. `uv tool uninstall coding-agent-notifier`, repeat step 4 — confirm shim prints install hint to stderr, session keeps running.

## References

- Plugin manifest schema: `code.claude.com/docs/en/plugins-reference.md`
- Marketplace creation: `code.claude.com/docs/en/plugin-marketplaces.md`
- Discovery / install UX: `code.claude.com/docs/en/discover-plugins.md`
