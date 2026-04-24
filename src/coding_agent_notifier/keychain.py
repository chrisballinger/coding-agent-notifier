"""macOS Keychain wrapper for agent-notify secrets.

Shells out to `/usr/bin/security` with a fixed service name ("agent-notify")
and per-secret account labels (e.g. "home:bot_token"). Follows a different
discipline from `macos.py`: because this lives on the credential path,
failures (timeout, missing binary, permission denied) must NOT be collapsed
to "not found" — that would silently mask real problems and send users into
confusing re-entry loops. Functions therefore distinguish three outcomes:

  1. **Found** — the value is returned.
  2. **Not found** — the account doesn't exist (`security` exit 44 =
     `errSecItemNotFound`). `read` returns `None`; `delete` returns `False`.
  3. **Failure** — any other error. `KeychainError` is raised.

Security note: `security add-generic-password` takes the password value on
argv (`-w <value>`). For the brief moment the subprocess runs, the value
is visible to `ps` for users with appropriate permissions. `security`
offers no stdin/file-descriptor password mode, so every wrapper around
it (including `keyring`'s macOS backend) makes the same trade-off. On a
single-user Mac this is not a practical issue; on a shared box, prefer
the env-var resolution path instead.
"""
from __future__ import annotations

import subprocess
import sys

SERVICE = "agent-notify"

# security(1) exit code when the requested item isn't in the keychain
# (errSecItemNotFound). Any other non-zero exit is treated as a real error.
_ITEM_NOT_FOUND_RC = 44

_TIMEOUT_SECONDS = 5


class KeychainError(RuntimeError):
    """Raised when `/usr/bin/security` fails for any reason other than
    "account not found" — missing binary, timeout, permission denied,
    unexpected exit code, or empty output with a zero exit."""


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_available() -> bool:
    """Probe: True if `/usr/bin/security` runs on this host. Never raises;
    for use by `doctor` and similar diagnostics."""
    if not is_macos():
        return False
    try:
        subprocess.run(
            ["/usr/bin/security", "-h"],
            check=False,
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return True


def read(account: str) -> str | None:
    """Return the stored secret, or None if the account doesn't exist.

    Raises KeychainError on any failure mode other than not-found (missing
    binary, timeout, permission denied, unexpected non-zero exit). Callers
    on the credential path should let the exception propagate — silently
    treating an I/O failure as "no token configured" masks the real issue.
    """
    if not is_macos():
        raise KeychainError("macOS Keychain is only available on darwin")
    result = _run(
        [
            "/usr/bin/security", "find-generic-password",
            "-s", SERVICE, "-a", account, "-w",
        ],
        op="find-generic-password",
    )
    if result.returncode == _ITEM_NOT_FOUND_RC:
        return None
    if result.returncode != 0:
        raise KeychainError(_format_error("find-generic-password", result))
    value = result.stdout.strip()
    if not value:
        raise KeychainError(
            "security find-generic-password returned exit 0 with empty output "
            f"for account {account!r} — Keychain state may be corrupted"
        )
    return value


def write(account: str, value: str) -> None:
    """Store or update `value` for `account`. Raises KeychainError on failure.

    Uses `-U` so the call is upsert; re-running the setup wizard is a no-op
    rather than an error.
    """
    if not value:
        raise ValueError("keychain.write requires a non-empty value")
    if not is_macos():
        raise KeychainError("macOS Keychain is only available on darwin")
    result = _run(
        [
            "/usr/bin/security", "add-generic-password",
            "-s", SERVICE, "-a", account, "-w", value, "-U",
        ],
        op="add-generic-password",
    )
    if result.returncode != 0:
        raise KeychainError(_format_error("add-generic-password", result))


def delete(account: str) -> bool:
    """Remove the entry. Returns True if deleted, False if it didn't exist.
    Raises KeychainError on real failures."""
    if not is_macos():
        raise KeychainError("macOS Keychain is only available on darwin")
    result = _run(
        [
            "/usr/bin/security", "delete-generic-password",
            "-s", SERVICE, "-a", account,
        ],
        op="delete-generic-password",
    )
    if result.returncode == 0:
        return True
    if result.returncode == _ITEM_NOT_FOUND_RC:
        return False
    raise KeychainError(_format_error("delete-generic-password", result))


def account_for(workspace: str, field: str) -> str:
    """Canonical keychain account label: `{workspace}:{field}`."""
    return f"{workspace}:{field}"


def _run(argv: list[str], *, op: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise KeychainError(f"/usr/bin/security not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise KeychainError(f"security {op} timed out after {_TIMEOUT_SECONDS}s") from e
    except subprocess.SubprocessError as e:
        raise KeychainError(f"security {op} failed: {e}") from e
    except OSError as e:
        raise KeychainError(f"security {op} failed: {e}") from e


def _format_error(op: str, result: subprocess.CompletedProcess) -> str:
    stderr = (result.stderr or "").strip()
    return (
        f"/usr/bin/security {op} exited {result.returncode}"
        + (f": {stderr}" if stderr else "")
    )
