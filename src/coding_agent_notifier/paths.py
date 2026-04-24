"""Central path resolution + permission-hardened file writes.

All state lives under a single dot dir (`~/.agent-notify/` by default) so
the user can back up, audit, or purge it in one place. Overridable via
`AGENT_NOTIFY_HOME`. Layout:

    ~/.agent-notify/              (0700)
    ├── config.toml               (0600)
    ├── state/                    (0700)
    │   ├── dedup.json
    │   ├── pending/
    │   └── approvals/
    └── logs/                     (0700)
        ├── defer.log
        └── daemon.log

Directory modes are enforced at creation *and* on every path access —
if a user somehow ends up with a 0755 `state/` dir, we tighten it back
rather than silently leaving the hole. Files are written through
`write_secure` which opens with `0o600` regardless of the caller's
umask.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

DIR_MODE = 0o700
FILE_MODE = 0o600

_ENV_HOME = "AGENT_NOTIFY_HOME"


def _legacy_config_dir() -> Path:
    return Path.home() / ".config" / "coding-agent-notifier"


def _legacy_cache_dir() -> Path:
    return Path.home() / ".cache" / "coding-agent-notifier"


# Back-compat: tests monkeypatch these attributes. Keep them as
# module-level callables resolved on each access.
_LEGACY_CONFIG_DIR = _legacy_config_dir()
_LEGACY_CACHE_DIR = _legacy_cache_dir()


def root() -> Path:
    """Return the dot directory, honoring `AGENT_NOTIFY_HOME`."""
    override = os.environ.get(_ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-notify"


def config_file() -> Path:
    return root() / "config.toml"


def state_dir() -> Path:
    return root() / "state"


def dedup_file() -> Path:
    return state_dir() / "dedup.json"


def pending_dir() -> Path:
    return state_dir() / "pending"


def approvals_dir() -> Path:
    return state_dir() / "approvals"


def logs_dir() -> Path:
    return root() / "logs"


def defer_log() -> Path:
    return logs_dir() / "defer.log"


def daemon_log() -> Path:
    return logs_dir() / "daemon.log"


# --- directory + file helpers -------------------------------------------


def ensure_dir(path: Path) -> Path:
    """Create `path` (and parents) with 0700, and re-tighten if any level
    already exists with a looser mode — but only within our own tree.

    `Path.mkdir(parents=True, mode=0o700)` only applies `mode` to the
    leaf, so we have to walk + chmod each level ourselves. Paths outside
    our dot tree are left with whatever mode they already have; we only
    tighten what we're managing.
    """
    # Create everything first (with umask defaults — we fix modes below).
    path.mkdir(parents=True, exist_ok=True)
    _tighten_within_tree(path)
    return path


def _tighten_within_tree(path: Path) -> None:
    """chmod `path` and every parent up to (and including) `root()` to
    0700. Stops at the tree boundary so we never chmod e.g. `/tmp`."""
    try:
        tree_root = root().resolve()
    except OSError:
        return
    try:
        current = path.resolve()
    except OSError:
        return
    chain: list[Path] = []
    p = current
    while True:
        chain.append(p)
        if p == tree_root:
            break
        if p.parent == p:
            # Walked off the top without hitting root — `path` is outside
            # our tree. Tighten only the leaf (best-effort) and stop.
            chain = [current]
            break
        p = p.parent
    for candidate in chain:
        try:
            cur_mode = candidate.stat().st_mode & 0o777
            if cur_mode != DIR_MODE:
                os.chmod(candidate, DIR_MODE)
        except OSError:
            pass


def write_secure(path: Path, content: str | bytes) -> None:
    """Atomic-replace write with 0600 perms, regardless of umask.

    Equivalent to `Path.write_text` but:
      - mode is enforced via `os.open(O_CREAT, 0o600)` so it doesn't
        inherit the process umask (which is typically 0022 → 0644);
      - written to a sibling `.tmp` then `os.replace`d for atomicity.
    """
    ensure_dir(path.parent)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def tighten(path: Path, *, file: bool = True) -> None:
    """chmod a path back to 0600/0700. No-op if the path is missing."""
    try:
        if not path.exists():
            return
        os.chmod(path, FILE_MODE if file else DIR_MODE)
    except OSError:
        pass


def open_append_secure(path: Path) -> "os.PathLike":
    """Open an append-mode file object with 0600 perms. Creates parent if
    needed. Caller is responsible for closing."""
    ensure_dir(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    return os.fdopen(fd, "a", encoding="utf-8")


# --- migration ----------------------------------------------------------


def migrate_legacy_state(*, stderr=None) -> list[str]:
    """One-time move from XDG locations into the new dot dir.

    Runs on every startup, but is a no-op after the first successful
    migration. Returns a list of human-readable messages describing what
    moved (empty list = nothing to do). Leaves the legacy directories
    in place so the user can inspect before removing them manually.
    """
    stderr = stderr if stderr is not None else sys.stderr
    dst_root = root()
    # If the new root already has content, we assume migration ran
    # previously. Re-running would risk clobbering fresh state.
    has_new_state = False
    for subdir in (state_dir(), logs_dir()):
        if subdir.exists() and any(subdir.iterdir()):
            has_new_state = True
            break
    if has_new_state and config_file().exists():
        return []

    # Tests monkeypatch _LEGACY_CONFIG_DIR / _LEGACY_CACHE_DIR to stub
    # locations; in production they point to ~/.config and ~/.cache.
    # Read via globals() so monkeypatch takes effect without re-import.
    legacy_config_dir = globals()["_LEGACY_CONFIG_DIR"]
    legacy_cache = globals()["_LEGACY_CACHE_DIR"]
    legacy_config = legacy_config_dir / "config.toml"
    if not legacy_config.exists() and not legacy_cache.exists():
        return []

    ensure_dir(dst_root)
    moved: list[str] = []

    if legacy_config.exists() and not config_file().exists():
        try:
            shutil.copy2(legacy_config, config_file())
            os.chmod(config_file(), FILE_MODE)
            moved.append(f"config.toml → {config_file()}")
        except OSError as e:
            print(f"agent-notify: could not migrate config.toml: {e}", file=stderr)

    if legacy_cache.exists():
        dst_state = ensure_dir(state_dir())
        dst_logs = ensure_dir(logs_dir())

        legacy_dedup = legacy_cache / "dedup.json"
        if legacy_dedup.exists() and not dedup_file().exists():
            _migrate_file(legacy_dedup, dedup_file(), moved, "dedup.json", stderr)

        legacy_pending = legacy_cache / "pending"
        if legacy_pending.exists() and not pending_dir().exists():
            _migrate_dir(legacy_pending, pending_dir(), moved, "pending/", stderr)

        # `pending_approvals/` is renamed to `approvals/` in the new layout.
        legacy_approvals = legacy_cache / "pending_approvals"
        if legacy_approvals.exists() and not approvals_dir().exists():
            _migrate_dir(legacy_approvals, approvals_dir(), moved,
                         "pending_approvals/ → approvals/", stderr)

        for log_name in ("defer.log", "daemon.log"):
            src = legacy_cache / log_name
            dst = logs_dir() / log_name
            if src.exists() and not dst.exists():
                _migrate_file(src, dst, moved, f"logs/{log_name}", stderr)

    if moved:
        print(
            f"agent-notify: migrated legacy state to {dst_root}/. "
            f"Moved: {', '.join(moved)}. Legacy paths "
            f"(~/.config/coding-agent-notifier, ~/.cache/coding-agent-notifier) "
            f"left in place — inspect and delete when you're ready.",
            file=stderr,
        )
    return moved


def _migrate_file(src: Path, dst: Path, moved: list[str], label: str, stderr) -> None:
    try:
        ensure_dir(dst.parent)
        shutil.copy2(src, dst)
        os.chmod(dst, FILE_MODE)
        moved.append(label)
    except OSError as e:
        print(f"agent-notify: could not migrate {label}: {e}", file=stderr)


def _migrate_dir(src: Path, dst: Path, moved: list[str], label: str, stderr) -> None:
    try:
        ensure_dir(dst)
        for child in src.iterdir():
            target = dst / child.name
            if target.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
            _tighten_tree(target)
        moved.append(label)
    except OSError as e:
        print(f"agent-notify: could not migrate {label}: {e}", file=stderr)


def _tighten_tree(root_path: Path) -> None:
    """Walk a newly-migrated tree and enforce 0700/0600."""
    if root_path.is_file():
        tighten(root_path, file=True)
        return
    tighten(root_path, file=False)
    for child in root_path.rglob("*"):
        if child.is_dir():
            tighten(child, file=False)
        else:
            tighten(child, file=True)
