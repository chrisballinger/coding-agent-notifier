from __future__ import annotations

import io
import os
from pathlib import Path

from coding_agent_notifier.config import load_config


def _write_mode(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def test_warns_on_loose_mode_with_inline_webhook(tmp_path: Path):
    config = tmp_path / "config.toml"
    _write_mode(config, 'webhook_url = "https://hooks.slack.com/test"\n', 0o644)
    stderr = io.StringIO()
    # The test actually loads the config — we just check the warning fired.
    load_config(config, stderr=stderr)
    text = stderr.getvalue()
    assert "mode 0o644" in text or "0o644" in text
    assert "chmod 600" in text
    assert str(config) in text


def test_warns_on_loose_mode_with_inline_bot_token(tmp_path: Path):
    config = tmp_path / "config.toml"
    _write_mode(config,
                '[slack.workspaces.default]\nbot_token = "xoxb-secret"\n',
                0o664)
    stderr = io.StringIO()
    load_config(config, stderr=stderr)
    assert "xoxb" not in stderr.getvalue()  # don't echo the secret
    assert "chmod 600" in stderr.getvalue()


def test_no_warning_when_0600(tmp_path: Path):
    config = tmp_path / "config.toml"
    _write_mode(config, '[slack.workspaces.default]\nbot_token = "xoxb-secret"\n', 0o600)
    stderr = io.StringIO()
    load_config(config, stderr=stderr)
    assert stderr.getvalue() == ""


def test_no_warning_when_no_inline_secrets(tmp_path: Path):
    """Loose perms on a config that only references env vars is fine —
    the file itself isn't the secret."""
    config = tmp_path / "config.toml"
    _write_mode(config,
                '[slack.workspaces.default]\nbot_token_env = "SLACK_BOT_TOKEN"\n',
                0o644)
    stderr = io.StringIO()
    load_config(config, stderr=stderr)
    assert stderr.getvalue() == ""


def test_no_warning_when_webhook_empty(tmp_path: Path):
    config = tmp_path / "config.toml"
    _write_mode(config,
                '[slack.workspaces.default]\nwebhook_url = ""\n',
                0o644)
    stderr = io.StringIO()
    load_config(config, stderr=stderr)
    assert stderr.getvalue() == ""


def test_warning_fires_for_app_token_too(tmp_path: Path):
    config = tmp_path / "config.toml"
    _write_mode(config,
                '[slack.workspaces.default]\napp_token = "xapp-secret"\n',
                0o644)
    stderr = io.StringIO()
    load_config(config, stderr=stderr)
    assert "chmod 600" in stderr.getvalue()
