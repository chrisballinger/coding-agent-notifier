from __future__ import annotations

from dataclasses import dataclass

from .config import Config, GatingMode
from .event import Event


@dataclass(frozen=True)
class SystemState:
    """Snapshot of the host state at decision time. None means "unknown"."""

    idle_seconds: float | None
    frontmost_app: str | None


def should_send(event: Event, config: Config, state: SystemState) -> bool:
    """Pure decision: given event + config + host state, send a notification?

    Rules:
      - If the event kind is disabled in config, never send.
      - Gating mode is taken per-event (with global fallback):
          always              → always send
          idle_only           → send iff idle ≥ threshold
          background_only     → send iff firing app is not frontmost
          idle_or_background  → send iff either idle or backgrounded
      - Unknown state (no idle reading, no frontmost) fails open — we err on the
        side of sending so the user hears about attention events even if the
        gating signals are broken.
    """
    ec = config.event(event.kind)
    if not ec.enabled:
        return False

    mode: GatingMode = config.gating_for(event.kind)
    if mode == "always":
        return True

    is_idle = _is_idle(state.idle_seconds, config.idle_threshold_seconds)
    is_backgrounded = _is_backgrounded(event.source_app, state.frontmost_app)

    if mode == "idle_only":
        return is_idle if is_idle is not None else True
    if mode == "background_only":
        return is_backgrounded if is_backgrounded is not None else True
    # idle_or_background
    if is_idle is None and is_backgrounded is None:
        return True
    return bool(is_idle) or bool(is_backgrounded)


def _is_idle(idle_seconds: float | None, threshold: float) -> bool | None:
    if idle_seconds is None:
        return None
    return idle_seconds >= threshold


def _is_backgrounded(source_app: str | None, frontmost: str | None) -> bool | None:
    if frontmost is None:
        return None
    if not source_app:
        # We don't know which app fired the hook — treat as backgrounded so the
        # user still gets pinged.
        return True
    return source_app != frontmost
