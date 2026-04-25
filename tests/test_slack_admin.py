from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
import tomllib

from coding_agent_notifier import keychain, slack_admin


# A global in-memory keychain replacement so tests don't touch the real one.
# Tests opt in via the `fake_keychain` fixture which also stubs is_macos to True.


class _FakeKeychain:
    def __init__(self):
        self.entries: dict[str, str] = {}
        self.write_should_fail: bool = False

    def read(self, account):
        return self.entries.get(account)

    def write(self, account, value):
        if not value:
            raise ValueError("empty value")
        if self.write_should_fail:
            raise keychain.KeychainError("simulated failure")
        self.entries[account] = value

    def delete(self, account):
        if account in self.entries:
            del self.entries[account]
            return True
        return False


@pytest.fixture
def fake_keychain(monkeypatch):
    fake = _FakeKeychain()
    monkeypatch.setattr(keychain, "read", fake.read)
    monkeypatch.setattr(keychain, "write", fake.write)
    monkeypatch.setattr(keychain, "delete", fake.delete)
    return fake


def _poster_ok_auth():
    def post(url, payload, *, headers=None, timeout=10.0):
        if url.endswith("/auth.test"):
            return 200, json.dumps({"ok": True, "team": "Acme Inc", "user_id": "U_BOT"})
        if url.endswith("/chat.postMessage"):
            return 200, json.dumps({"ok": True, "channel": "D_BOT", "ts": "1.0"})
        return 200, json.dumps({"ok": True})
    return post


def _poster_auth_fail(error: str = "invalid_auth"):
    def post(url, payload, *, headers=None, timeout=10.0):
        return 200, json.dumps({"ok": False, "error": error})
    return post


# ---------------------------------------------------------------------
# run_add_wizard
# ---------------------------------------------------------------------


def test_add_wizard_non_interactive_writes_config_and_keychain(tmp_path, fake_keychain):
    config_path = tmp_path / "config.toml"
    stdout = io.StringIO()
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-real",
        app_token="xapp-real",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=stdout,
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    assert rc == 0
    # Keychain stores both tokens under the workspace-prefixed account.
    assert fake_keychain.entries["work:bot_token"] == "xoxb-real"
    assert fake_keychain.entries["work:app_token"] == "xapp-real"
    # config.toml has the workspace block referencing Keychain accounts.
    raw = tomllib.loads(config_path.read_text())
    block = raw["slack"]["workspaces"]["work"]
    assert block["enabled"] is True
    assert block["bot_token_keychain"] == "work:bot_token"
    assert block["app_token_keychain"] == "work:app_token"
    assert block["interactive"] is True
    assert block["actionable_approvals"] is True
    assert block["channel"] == "@me"
    # No approvers → DM-only is implicit (and valid at load time).
    assert "approver_user_ids" not in block


def test_add_wizard_stores_approvers_when_provided(tmp_path, fake_keychain):
    config_path = tmp_path / "config.toml"
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-real",
        app_token="xapp-real",
        channel="#agents",
        approvers="U01, U02, U03",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    assert rc == 0
    raw = tomllib.loads(config_path.read_text())
    block = raw["slack"]["workspaces"]["work"]
    assert block["approver_user_ids"] == ["U01", "U02", "U03"]
    assert block["channel"] == "#agents"


def test_add_wizard_warns_when_bot_token_format_looks_wrong(tmp_path, fake_keychain):
    """A common typo is swapping the bot/app tokens. The wizard soft-warns
    when bot_token doesn't start with xoxb- so the user notices before the
    daemon's first connection attempt fails opaquely later."""
    config_path = tmp_path / "config.toml"
    stderr = io.StringIO()
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xapp-mistyped",  # wrong prefix
        app_token="xapp-real",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=stderr,
        poster=_poster_ok_auth(),
        no_verify=True,  # skip auth.test so we test the prefix warning, not the API
    )
    assert "doesn't start with 'xoxb-'" in stderr.getvalue()


def test_add_wizard_warns_when_app_token_format_looks_wrong(tmp_path, fake_keychain):
    config_path = tmp_path / "config.toml"
    stderr = io.StringIO()
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-real",
        app_token="xoxb-also-wrong",  # wrong prefix for app token
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=stderr,
        poster=_poster_ok_auth(),
        no_verify=True,
    )
    assert "doesn't start with 'xapp-'" in stderr.getvalue()


def test_add_wizard_no_warning_for_valid_token_prefixes(tmp_path, fake_keychain):
    config_path = tmp_path / "config.toml"
    stderr = io.StringIO()
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-real",
        app_token="xapp-real",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=stderr,
        poster=_poster_ok_auth(),
        no_verify=True,
    )
    assert "doesn't start with" not in stderr.getvalue()


def test_add_wizard_warns_on_shared_channel_without_approvers(
    tmp_path, fake_keychain
):
    config_path = tmp_path / "config.toml"
    stderr = io.StringIO()
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb",
        app_token="xapp",
        channel="#public",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=stderr,
        poster=_poster_ok_auth(),
    )
    assert rc == 0  # wizard completes but warns
    assert "not a DM and no approver_user_ids" in stderr.getvalue()


def test_add_wizard_auth_test_failure_rolls_back_nothing(tmp_path, fake_keychain):
    """auth.test runs BEFORE Keychain writes, so a failure leaves no partial state."""
    config_path = tmp_path / "config.toml"
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-bad",
        app_token="xapp",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_auth_fail("invalid_auth"),
    )
    assert rc == 1
    assert fake_keychain.entries == {}
    assert not config_path.exists()


def test_add_wizard_keychain_failure_rolls_back_already_written(
    tmp_path, fake_keychain
):
    """If writing the app_token fails mid-sequence, the bot_token write is
    rolled back so the next wizard run starts clean."""
    config_path = tmp_path / "config.toml"
    call_count = {"n": 0}
    real_write = fake_keychain.write

    def failing_write(account, value):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise keychain.KeychainError("disk full or whatever")
        real_write(account, value)

    import coding_agent_notifier.keychain as kc
    orig = kc.write
    kc.write = failing_write
    try:
        rc = slack_admin.run_add_wizard(
            name="work",
            bot_token="xoxb-real",
            app_token="xapp-real",
            channel="@me",
            approvers="",
            config_path=config_path,
            stdin=io.StringIO(""),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            poster=_poster_ok_auth(),
        )
    finally:
        kc.write = orig
    assert rc == 1
    # The bot_token write succeeded, then app_token failed — bot_token should
    # have been deleted by the rollback.
    assert fake_keychain.entries == {}


def test_add_wizard_upsert_preserves_other_workspaces(tmp_path, fake_keychain):
    """Running the wizard a second time for a DIFFERENT workspace must not
    clobber the first one."""
    config_path = tmp_path / "config.toml"
    # First workspace
    slack_admin.run_add_wizard(
        name="home",
        bot_token="xoxb-home",
        app_token="xapp-home",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    # Second workspace
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-work",
        app_token="xapp-work",
        channel="#work",
        approvers="U01",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    raw = tomllib.loads(config_path.read_text())
    assert set(raw["slack"]["workspaces"].keys()) == {"home", "work"}
    assert raw["slack"]["workspaces"]["home"]["channel"] == "@me"
    assert raw["slack"]["workspaces"]["work"]["channel"] == "#work"


def test_add_wizard_reupsert_replaces_same_workspace(tmp_path, fake_keychain):
    """Re-running the wizard on the same name replaces the block cleanly
    (no stale keys left behind from a prior run)."""
    config_path = tmp_path / "config.toml"
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-v1",
        app_token="xapp-v1",
        channel="#v1",
        approvers="U_V1",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    # Re-run without approvers, different channel
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-v2",
        app_token="xapp-v2",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    raw = tomllib.loads(config_path.read_text())
    block = raw["slack"]["workspaces"]["work"]
    assert block["channel"] == "@me"
    # Old U_V1 approver shouldn't linger — the block is rebuilt from scratch.
    assert "approver_user_ids" not in block


def test_add_wizard_no_verify_skips_auth_test(tmp_path, fake_keychain):
    """--no-verify short-circuits the auth.test call so a user without
    network can still bootstrap."""
    config_path = tmp_path / "config.toml"
    calls: list[str] = []

    def poster(url, *a, **kw):
        calls.append(url)
        return 200, json.dumps({"ok": True})

    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb",
        app_token="xapp",
        channel="@me",
        approvers="",
        no_verify=True,
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=poster,
    )
    assert rc == 0
    assert calls == []  # no auth.test call


def test_add_wizard_interactive_stdin_prompts(tmp_path, fake_keychain):
    """With no CLI flags, wizard reads name / channel / approvers from stdin
    and tokens via the injected prompt_password."""
    config_path = tmp_path / "config.toml"
    tokens = iter(["xoxb-i", "xapp-i"])

    def fake_getpass(prompt):
        return next(tokens)

    stdin = io.StringIO("work\n@me\n\n")  # name, channel, approvers (blank)
    stdout = io.StringIO()
    rc = slack_admin.run_add_wizard(
        config_path=config_path,
        stdin=stdin,
        stdout=stdout,
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
        prompt_password=fake_getpass,
    )
    assert rc == 0
    assert fake_keychain.entries["work:bot_token"] == "xoxb-i"
    assert fake_keychain.entries["work:app_token"] == "xapp-i"
    raw = tomllib.loads(config_path.read_text())
    assert "work" in raw["slack"]["workspaces"]


def test_add_wizard_reads_token_from_stdin_on_dash(tmp_path, fake_keychain):
    """`--bot-token -` reads one line from stdin — for scripted install
    from a password manager that pipes the token in."""
    config_path = tmp_path / "config.toml"
    stdin = io.StringIO("xoxb-piped\nxapp-piped\n")
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="-",
        app_token="-",
        channel="@me",
        approvers="",
        config_path=config_path,
        stdin=stdin,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    assert rc == 0
    assert fake_keychain.entries["work:bot_token"] == "xoxb-piped"


def test_add_wizard_empty_bot_token_fails(tmp_path, fake_keychain):
    stderr = io.StringIO()
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="",
        app_token="",
        channel="@me",
        approvers="",
        no_verify=True,
        config_path=tmp_path / "config.toml",
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=stderr,
        poster=_poster_ok_auth(),
    )
    assert rc == 1
    assert "bot token is required" in stderr.getvalue()


def test_add_wizard_no_actionable_flag_omits_actionable_field(
    tmp_path, fake_keychain
):
    config_path = tmp_path / "config.toml"
    rc = slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb",
        app_token="xapp",
        channel="@me",
        approvers="",
        no_actionable=True,
        no_verify=True,
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    assert rc == 0
    raw = tomllib.loads(config_path.read_text())
    block = raw["slack"]["workspaces"]["work"]
    # `interactive` still gets set when app_token is present, but
    # actionable_approvals stays off.
    assert block["interactive"] is True
    assert "actionable_approvals" not in block


# ---------------------------------------------------------------------
# list_workspaces
# ---------------------------------------------------------------------


def test_list_workspaces_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    ws = slack_admin.list_workspaces()
    assert ws == []


def test_list_workspaces_populated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text("""
[slack.workspaces.home]
enabled = true
bot_token = "xoxb-h"
channel = "@me"

[slack.workspaces.work]
enabled = true
bot_token = "xoxb-w"
app_token = "xapp-w"
actionable_approvals = true
approver_user_ids = ["U01"]
channel = "#work"
""".strip())
    os.chmod(config, 0o600)
    ws_list = slack_admin.list_workspaces()
    by_name = {w.name: w for w in ws_list}
    assert set(by_name) == {"home", "work"}
    assert by_name["work"].actionable_approvals is True
    assert by_name["work"].approver_user_ids == ("U01",)
    assert by_name["home"].has_bot_token is True
    assert by_name["home"].has_app_token is False


# ---------------------------------------------------------------------
# remove_workspace
# ---------------------------------------------------------------------


def test_remove_workspace_clears_config_and_keychain(
    tmp_path, fake_keychain, monkeypatch
):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    # Bootstrap via the wizard so we have a realistic block + Keychain state.
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb",
        app_token="xapp",
        channel="@me",
        approvers="",
        no_verify=True,
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    assert "work:bot_token" in fake_keychain.entries

    summary = slack_admin.remove_workspace("work", config_path=config_path)
    assert summary["config_removed"] is True
    assert set(summary["keychain_removed"]) == {"work:bot_token", "work:app_token"}
    raw = tomllib.loads(config_path.read_text())
    assert "work" not in raw.get("slack", {}).get("workspaces", {})
    assert fake_keychain.entries == {}


def test_remove_workspace_is_idempotent(tmp_path, fake_keychain):
    config_path = tmp_path / "config.toml"
    summary = slack_admin.remove_workspace("ghost", config_path=config_path)
    assert summary["config_removed"] is False
    assert summary["keychain_removed"] == []


# ---------------------------------------------------------------------
# test_workspace
# ---------------------------------------------------------------------


def test_test_workspace_posts_to_channel(tmp_path, fake_keychain, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    slack_admin.run_add_wizard(
        name="work",
        bot_token="xoxb-secret",
        app_token="xapp-secret",
        channel="#test",
        approvers="U01",
        no_verify=True,
        config_path=config_path,
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poster=_poster_ok_auth(),
    )
    calls: list[dict] = []

    def poster(url, payload, *, headers=None, timeout=10.0):
        calls.append({"url": url, "payload": payload, "headers": headers})
        return 200, json.dumps({"ok": True, "channel": "#test", "ts": "1.0"})

    ok, msg = slack_admin.test_workspace("work", poster=poster)
    assert ok is True
    assert "#test" in msg
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/chat.postMessage")
    assert calls[0]["headers"]["Authorization"] == "Bearer xoxb-secret"


def test_test_workspace_unknown_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    ok, msg = slack_admin.test_workspace("ghost")
    assert ok is False
    assert "unknown workspace" in msg


def test_test_workspace_bot_token_missing(tmp_path, monkeypatch):
    """Workspace exists in config but token can't be resolved (no Keychain
    entry, no env var, no inline) — we should report the specific error
    rather than silently pretending the workspace is unconfigured."""
    monkeypatch.setenv("AGENT_NOTIFY_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    # Write a workspace with only app_token, deliberately no bot_token.
    config_path.write_text("""
[slack.workspaces.work]
enabled = true
webhook_url = "https://hooks.slack.com/services/x"
""".strip())
    os.chmod(config_path, 0o600)
    ok, msg = slack_admin.test_workspace("work")
    assert ok is False
    assert "no resolved bot_token" in msg
