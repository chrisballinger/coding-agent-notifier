"""Approval registry shared by hooks and the Slack Socket Mode daemon.

Flow:
    1. Claude Code fires `PreToolUse`. The hook calls `create(approval_id, …)`,
       which writes a JSON record and creates a named FIFO.
    2. The hook posts a Slack message with approve/deny buttons carrying the
       same `approval_id`, stashes the message `(channel, ts)` via
       `set_message_ref`, and blocks on `wait(approval_id, timeout=…)`.
    3. The daemon receives the button click, calls `resolve(approval_id,
       decision, actor=…)`. That writes the decision into the record and
       unblocks the FIFO (writer open + close is enough — the reader's
       `select` returns readable).
    4. Hook's `wait` returns the decision string; hook emits Claude Code's
       expected JSON and exits. `cleanup` unlinks the record + FIFO.

Mirrors `pending.py` for locking and serialization; diverges in that each
approval has its own `{approval_id}.json` (not keyed by session) and an
accompanying `{approval_id}.fifo`.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import select
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


def default_approvals_dir() -> Path:
    from . import paths
    return paths.approvals_dir()


def _record_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.json"


def _fifo_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.fifo"


def _lock_path(approval_id: str, base_dir: Path | None) -> Path:
    return (base_dir or default_approvals_dir()) / f"{_safe(approval_id)}.lock"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def create(
    approval_id: str,
    *,
    agent: str,
    session_id: str | None,
    tool_name: str | None,
    tool_input: dict | None = None,
    workspace: str = "default",
    permission_suggestions: list[dict] | None = None,
    tool_use_id: str | None = None,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> Path:
    """Write the initial record and mkfifo the wake pipe. Returns record path.

    `workspace` names the Slack workspace the message was posted to. The
    hook's timeout-update path reads it back to pick the right bot_token
    for `chat.update`; older records that predate this field default to
    "default" at read time.

    `permission_suggestions` is the PermissionRequest payload's suggestion
    list — stored on the record so the daemon can look it up at click time
    (avoids re-shipping it through the action_id value).

    `tool_use_id` is Claude Code's per-call identifier for the tool
    invocation. Stored so the PostToolUse back-fill path can find the
    matching record without a session-wide scan.
    """
    record = _record_path(approval_id, base_dir)
    fifo = _fifo_path(approval_id, base_dir)
    record.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "approval_id": approval_id,
        "agent": agent,
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input if isinstance(tool_input, dict) else None,
        "tool_use_id": tool_use_id,
        "workspace": workspace,
        "created_at": clock(),
        "decision": None,
        "actor": None,
        "resolved_at": None,
        "channel": None,
        "message_ts": None,
        # Single-question AskUserQuestion legacy field — index into
        # tool_input["questions"][0]["options"]. Kept for back-compat with
        # in-flight approvals from older versions; new code prefers
        # `selected_options` below.
        "selected_option_index": None,
        # Multi-question AskUserQuestion answers, keyed by question index
        # (str — JSON keys must be strings) → selected option index. Filled
        # incrementally as the user taps buttons; resolution fires only
        # when every question has an entry. Stays empty for non-AskUser-
        # Question approvals.
        "selected_options": {},
        # PermissionRequest's permission_suggestions payload (the rule
        # edits Claude Code would offer the user). Stored verbatim so the
        # daemon can look up which suggestion was clicked. None when the
        # harness didn't ship any.
        "permission_suggestions": (
            list(permission_suggestions) if permission_suggestions else None
        ),
        # Index into permission_suggestions when the user clicked a
        # suggestion button (vs plain Approve/Deny). The hook uses this
        # to build PermissionRequest's `decision.updatedPermissions`.
        "selected_suggestion_index": None,
        # Per-question freeform answers from the "Custom answer" modal,
        # keyed by question index (str). Wins over `selected_options[k]`
        # at resolve time — the hook surfaces the typed text directly via
        # PermissionRequest's `updatedInput.answers`.
        "freeform_answers": {},
        # User-typed reason from the "Deny with reason" modal. Plumbed
        # into `decision.message` on the deny path. None for one-tap deny.
        "deny_reason": None,
    }
    with _locked(_lock_path(approval_id, base_dir)):
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(payload))
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
    return record


def set_message_ref(
    approval_id: str,
    channel: str,
    message_ts: str,
    *,
    base_dir: Path | None = None,
) -> None:
    """Remember the Slack message (channel, ts) so `resolve` can edit it."""
    record = _record_path(approval_id, base_dir)
    with _locked(_lock_path(approval_id, base_dir)):
        if not record.exists():
            return
        data = json.loads(record.read_text())
        data["channel"] = channel
        data["message_ts"] = message_ts
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(data))


def resolve(
    approval_id: str,
    decision: str,
    *,
    actor: str | None = None,
    selected_option: int | None = None,
    selected_options: dict[str, int] | None = None,
    selected_suggestion: int | None = None,
    freeform_answers: dict[str, str] | None = None,
    deny_reason: str | None = None,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> dict | None:
    """Mark the approval resolved and wake the waiting hook. Returns record or None.

    `decision` is one of: "allow", "deny", "timed_out". The first two come
    from a Slack click or modal submission and produce a real Claude Code
    decision. "timed_out" is recorded by the hook itself when the Slack
    wait elapsed without a click — it marks the record as no-longer-
    actionable so concurrent Slack clicks become idempotent no-ops, while
    leaving the door open for a later PostToolUse back-fill (which calls
    `resolve` again with "allow"/"deny" and the answers from another
    surface).

    `selected_option` (legacy single-question AskUserQuestion) is the index
    into `tool_input["questions"][0]["options"]`. New multi-question code
    passes `selected_options` instead — a dict mapping question index (str)
    → selected option index. Both flow into PermissionRequest's
    `updatedInput.answers` via `cmd_permissionrequest`.
    """
    if decision not in ("allow", "deny", "timed_out"):
        raise ValueError(f"decision must be 'allow', 'deny', or 'timed_out', got {decision!r}")
    record = _record_path(approval_id, base_dir)
    fifo = _fifo_path(approval_id, base_dir)
    with _locked(_lock_path(approval_id, base_dir)):
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        existing = data.get("decision")
        # "timed_out" records are intentionally upgradable to a real decision:
        # the hook timed out and fell through to the TUI, then the user
        # answered there and PostToolUse called us back with the actual
        # answer. Real decisions (allow/deny) are still idempotent.
        if existing in ("allow", "deny"):
            return data
        if existing == "timed_out" and decision == "timed_out":
            return data
        data["decision"] = decision
        data["actor"] = actor
        data["resolved_at"] = clock()
        if selected_option is not None:
            data["selected_option_index"] = selected_option
        if selected_options is not None:
            data["selected_options"] = {str(k): int(v) for k, v in selected_options.items()}
        if selected_suggestion is not None:
            data["selected_suggestion_index"] = int(selected_suggestion)
        if freeform_answers is not None:
            data["freeform_answers"] = {str(k): str(v) for k, v in freeform_answers.items()}
        if deny_reason is not None:
            data["deny_reason"] = str(deny_reason)
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(data))
    # Wake outside the lock: opening a FIFO for write blocks until a reader
    # is open, and the reader path below also tries to lock — keeping the
    # write-open inside `_locked` would deadlock.
    _kick(fifo)
    return data


def record_partial_answer(
    approval_id: str,
    question_index: int,
    *,
    option_index: int | None = None,
    text: str | None = None,
    actor: str | None = None,
    base_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> dict | None:
    """Record one question's answer (option click OR freeform text) without
    resolving the whole approval. Returns the updated record or None if it
    doesn't exist / is already resolved.

    Exactly one of `option_index` / `text` must be provided. Per question,
    text and option are mutually exclusive — submitting one clears the
    other so the resolve path doesn't have to disambiguate.
    """
    if (option_index is None) == (text is None):
        raise ValueError("record_partial_answer needs exactly one of option_index / text")
    record = _record_path(approval_id, base_dir)
    with _locked(_lock_path(approval_id, base_dir)):
        if not record.exists():
            return None
        data = json.loads(record.read_text())
        if data.get("decision") is not None:
            # Already fully resolved — partial updates are pointless.
            return data
        key = str(question_index)
        existing_opts = data.get("selected_options")
        selected_options = dict(existing_opts) if isinstance(existing_opts, dict) else {}
        existing_text = data.get("freeform_answers")
        freeform_answers = dict(existing_text) if isinstance(existing_text, dict) else {}
        if option_index is not None:
            selected_options[key] = int(option_index)
            freeform_answers.pop(key, None)
        else:
            freeform_answers[key] = str(text)
            selected_options.pop(key, None)
        data["selected_options"] = selected_options
        data["freeform_answers"] = freeform_answers
        # Stamp the most-recent actor so the chat.update can attribute the
        # partial click. `actor` on a fully-resolved record still wins.
        if actor is not None:
            data["actor"] = actor
        from . import paths as _paths
        _paths.write_secure(record, json.dumps(data))
        return data


def _kick(fifo: Path) -> None:
    if not fifo.exists():
        return
    try:
        fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        # ENXIO = no reader yet. The reader will see the record on its own
        # when it next checks; our wake signal is best-effort.
        if e.errno == errno.ENXIO:
            return
        return
    try:
        try:
            os.write(fd, b"x")
        except OSError:
            pass
    finally:
        os.close(fd)


def read(approval_id: str, *, base_dir: Path | None = None) -> dict | None:
    record = _record_path(approval_id, base_dir)
    if not record.exists():
        return None
    try:
        return json.loads(record.read_text())
    except (OSError, ValueError):
        return None


def wait(
    approval_id: str,
    *,
    timeout: float | None,
    base_dir: Path | None = None,
    poll_interval: float = 1.0,
) -> dict | None:
    """Block until a decision arrives, returning the resolved record dict.

    `timeout` is the wait budget in seconds; pass `None` to wait forever
    (no deadline — useful when the user wants the approval to remain
    pending until manually resolved from any device, rather than failing
    closed). Returns `None` only when a finite timeout elapses without a
    decision.

    Polls the record file in addition to select'ing on the FIFO so we
    recover if the daemon wrote the decision before the FIFO open (race)
    or if the FIFO write was dropped (ENXIO).
    """
    fifo = _fifo_path(approval_id, base_dir)
    if not fifo.exists():
        fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo, 0o600)
    deadline = None if timeout is None else time.monotonic() + timeout
    fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    try:
        while True:
            # Check record first — handles the resolve-before-wait race.
            rec = read(approval_id, base_dir=base_dir)
            if rec and rec.get("decision") in ("allow", "deny"):
                return rec
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                select_timeout = min(remaining, poll_interval)
            else:
                select_timeout = poll_interval
            ready, _, _ = select.select([fd], [], [], select_timeout)
            if ready:
                try:
                    os.read(fd, 64)
                except OSError:
                    pass
                rec = read(approval_id, base_dir=base_dir)
                if rec and rec.get("decision") in ("allow", "deny"):
                    return rec
                # Spurious wake (writer opened and closed with no decision
                # written yet) — loop and re-check.
    finally:
        os.close(fd)


def cleanup(approval_id: str, *, base_dir: Path | None = None) -> None:
    """Remove the record, FIFO, and lock. Best-effort; swallows errors."""
    for path in (
        _record_path(approval_id, base_dir),
        _fifo_path(approval_id, base_dir),
        _lock_path(approval_id, base_dir),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def list_pending(*, base_dir: Path | None = None) -> list[dict]:
    """Scan all unresolved records. Used by `agent-notify status` and by
    the daemon on startup to recover from a crash."""
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("decision") is None:
            out.append(data)
    return out


def find_by_tool_use_id(
    tool_use_id: str,
    *,
    base_dir: Path | None = None,
) -> dict | None:
    """Return the most recent record matching `tool_use_id`, or None.

    Used by the PostToolUse back-fill path: when Claude Code reports that
    a tool finished, we need the original approval record (to know which
    Slack message to update). Scans all records — there's no on-disk
    index, but the approvals directory is small in practice (recently-
    completed records get garbage-collected via `gc_stale`).

    Includes already-resolved records so a PostToolUse arriving just
    after a `timed_out` mark can still find the original.
    """
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return None
    best: dict | None = None
    best_created = -1.0
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("tool_use_id") != tool_use_id:
            continue
        created = data.get("created_at")
        c = float(created) if isinstance(created, (int, float)) else 0.0
        if c > best_created:
            best = data
            best_created = c
    return best


def find_active_for_session(
    session_id: str,
    tool_name: str,
    *,
    base_dir: Path | None = None,
) -> dict | None:
    """Fallback lookup when `tool_use_id` is unavailable.

    Returns the most recent record (by `created_at`) for `(session_id,
    tool_name)` whose decision is None or "timed_out" — i.e. records
    that haven't been finalized via Slack. Used by the PostToolUse
    back-fill for older approvals stored before `tool_use_id` was
    persisted.
    """
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return None
    best: dict | None = None
    best_created = -1.0
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("session_id") != session_id:
            continue
        if data.get("tool_name") != tool_name:
            continue
        if data.get("decision") in ("allow", "deny"):
            continue
        created = data.get("created_at")
        c = float(created) if isinstance(created, (int, float)) else 0.0
        if c > best_created:
            best = data
            best_created = c
    return best


def gc_stale(*, max_age_seconds: float = 3600.0, base_dir: Path | None = None,
             clock: Callable[[], float] = time.time) -> int:
    """Remove records older than `max_age_seconds`. Returns count removed."""
    root = base_dir or default_approvals_dir()
    if not root.exists():
        return 0
    now = clock()
    removed = 0
    for path in list(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        created = data.get("created_at") if isinstance(data, dict) else None
        if not isinstance(created, (int, float)):
            continue
        if now - created < max_age_seconds:
            continue
        aid = data.get("approval_id")
        if isinstance(aid, str):
            cleanup(aid, base_dir=base_dir)
            removed += 1
    return removed
