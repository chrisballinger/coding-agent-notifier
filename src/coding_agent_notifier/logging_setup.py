"""Centralised logging setup.

Two consumer profiles:

  * Short-lived CLI commands (`hook`, `doctor`, `slack add`, …) — stderr
    handler only. Default level is WARNING so the hook stays silent on
    happy paths; `--debug` flips it to DEBUG for troubleshooting. Hook
    payload audit lines still go to `~/.agent-notify/logs/defer.log` via
    cli._log_event (separate, append-only path that's cheap and
    crash-safe).

  * The long-lived `daemon` command — same stderr handler PLUS a
    RotatingFileHandler at `paths.daemon_log()` (10 MiB × 3 backups) so
    a daemon supervised by launchd has a bounded on-disk log without
    relying on launchd's own redirect.

Calling `configure` more than once is safe: it idempotently replaces
the handlers it owns, leaving any third-party handlers untouched.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths

_HANDLER_TAG = "agent-notify"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure(*, debug: bool = False, daemon: bool = False) -> None:
    """Wire up handlers on the root logger.

    `debug=True` lowers the level to DEBUG; otherwise WARNING (so a quiet
    hook stays quiet). `daemon=True` adds the rotating file handler at
    `paths.daemon_log()` in addition to stderr.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.WARNING)
    # Drop any handlers we previously installed so re-calling is idempotent.
    root.handlers = [h for h in root.handlers if getattr(h, "_agent_notify", False) is False]

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    stderr_handler._agent_notify = True  # type: ignore[attr-defined]
    root.addHandler(stderr_handler)

    if daemon:
        log_path = paths.daemon_log()
        paths.ensure_dir(log_path.parent)
        # 10 MiB × 3 backups caps disk use at ~40 MiB even under a tight
        # respawn loop or a chatty Slack API. Plenty for forensic context
        # without growing without bound.
        file_handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        file_handler._agent_notify = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
