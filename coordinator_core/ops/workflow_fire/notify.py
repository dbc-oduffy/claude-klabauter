"""workflow_fire.notify -- one-shot completion message from a finished fire to its ``notify_to`` roster identity.

``notify_terminal`` routes through ``agent_pipe.bridge`` (claude mailbox, codex/agy/grok legs).
Codex notices wait for persistent queue acceptance, never a remote turn; agy/grok hand off detached.
Trap: a ``handed-off`` Delivery means the leg started, not that the peer received the text; a later
``leg-failed`` lands in bridge.log.jsonl. ``notify_status`` records the Delivery status verbatim.
``queued`` means Codex accepted the notice in its inbox, not that its TUI consumed it.
Legacy notices report driver exit, not workflow completion. Headless notices use
the persisted result/landing truth after its writer releases notification ownership.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from uuid import NAMESPACE_URL, uuid5
from typing import Any
from pathlib import Path

from coordinator_core.agent_pipe import bridge

FROM_IDENTITY = "workflow.fire"
_NON_TERMINAL = ("pending", "running")


def _text(record: dict) -> str:
    exit_code = record.get("exit_code")
    if record.get("executor") == "headless":
        if record.get("outcome_basis") == "headless preflight failed before execution":
            return f"Workflow failed before execution: fire_id={record['fire_id']} error={record.get('error')}"
        label = "Workflow incomplete"
        if record.get("state") == "exited" and exit_code is not None:
            if exit_code == 0 and record.get("outcome") == "clean":
                label = "Workflow completed"
            elif record.get("outcome") == "failed":
                label = "Workflow failed"
        return (f"{label}: fire_id={record['fire_id']} exit_code={exit_code} "
                f"outcome={record.get('outcome', 'unknown')} journal={record.get('journal_path')} "
                f"landing={json.dumps(record.get('landing'))}")
    if record.get("execution_started") is None and record.get("execution_note"):
        return (f"workflow.fire reservation expired (execution-unknown): fire_id={record.get('fire_id')}. "
                f"{record['execution_note']}")
    return (
        f"workflow.fire driver finished (driver-done, not run-completed): "
        f"fire_id={record.get('fire_id')} state={record.get('state')} "
        f"exit_code={'unknown' if exit_code is None else exit_code} "
        f"log={record.get('log_path') or 'unknown'}. "
        f"Check workflow.fire_status({record.get('fire_id')}) for the run outcome."
    )


def _headless_owner_live(record: dict) -> bool:
    """Defer to an active writer; an explicit release transfers notification ownership."""
    import psutil

    if record.get("notify_owner_released") is True:
        return False
    pid = record.get("notify_owner_pid", record.get("pid"))
    birth = record.get("notify_owner_birth", record.get("pid_birth"))
    if type(pid) is not int or pid <= 0:
        return False
    try:
        owner = psutil.Process(pid)
        if not owner.is_running() or owner.status() == psutil.STATUS_ZOMBIE:
            return False
        if birth is not None:
            if type(birth) not in (int, float):
                return True  # Malformed identity is unknown, not proof the writer died.
            if owner.create_time() != birth:
                return False
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _refresh_headless(record: dict) -> dict:
    if _headless_owner_live(record) or record.get("state") not in _NON_TERMINAL:
        return record
    if record.get("state") == "pending":
        import time
        from coordinator_core.ops.workflow_fire import fire
        if time.time() - record.get("reserved_at", 0) <= fire._PENDING_RECLAIM_TIMEOUT_S:
            return record
    return {**record, "state": "exited", "exit_code": None, "outcome": "unknown",
            "outcome_basis": "headless owner exited before terminal checkpoint"}


def notify_terminal(record: dict) -> dict:
    """Send one bridge message to ``record['notify_to']``; return a copy with ``notified_at``/``notify_status`` set.

    No-op (record returned unchanged) when ``notify_to`` is absent, ``notified_at`` is already set, or the
    state is pending/running, or a headless writer still owns the record.
    ``refused``/``unroutable`` deliveries and any exception set ``notify_error``
    and leave ``notified_at`` unset. Never raises.
    """
    try:
        target = record.get("notify_to")
        if not target or record.get("notified_at") or record.get("state") in _NON_TERMINAL:
            return record
        if record.get("executor") == "headless" and _headless_owner_live(record):
            return record
        out: dict[str, Any] = dict(record)
        message = bridge.make_message(FROM_IDENTITY, target, _text(record))
        message = replace(message, id=uuid5(NAMESPACE_URL, f"workflow.fire:{record['fire_id']}:{target}").hex)
        delivery = bridge.route(message)
        out["notify_status"] = delivery.status
        if delivery.status in ("refused", "unroutable"):
            out["notify_error"] = f"{delivery.status}: {delivery.reason}"
        else:
            out["notified_at"] = datetime.now(timezone.utc).isoformat()
            out.pop("notify_error", None)
        return out
    except Exception as exc:  # noqa: BLE001 - a notify failure must never break fire_status
        failed = dict(record)
        failed["notify_error"] = f"{type(exc).__name__}: {exc}"
        return failed


def refresh_record(path: Path, record: dict, *, notify: bool = True) -> dict:
    """Refresh/persist a fire and serialize terminal delivery across processes.

    A busy notifier is skipped immediately, never waited on. Kernel-backed locking
    releases on process death on both Windows and POSIX; the record remains retryable.
    """
    from coordinator_core.locked_write import LockTimeout, held_lock
    from coordinator_core.ops.workflow_fire import fire

    def persist(value):
        if value.get("executor") == "headless":
            # Preserve engine-authored run/landing truth instead of parsing a Claude log.
            from coordinator_core.ops.workflow_fire import headless
            headless._save(path, value)
            return value
        return fire._write_record(path, value)

    def update(current):
        if current.get("executor") == "headless":
            if _headless_owner_live(current):
                return current
            # The writer can finish after the initial read but before its death probe.
            current = fire._read_record(path) or current
            refreshed = _refresh_headless(current)
        else:
            refreshed = fire._refresh_record_state(current)
        if refreshed is not current:
            refreshed = persist(refreshed)
        if notify and refreshed.get("notify_to") and not refreshed.get("notified_at") and refreshed.get("state") == "exited":
            notified = notify_terminal(refreshed)
            if notified != refreshed:
                return persist(notified)
        return refreshed

    # Settled unaddressed history needs no write or lock on a registry sweep.
    if record.get("executor") != "headless" and not record.get("notify_to") and fire._refresh_record_state(record) is record:
        return record
    try:
        with held_lock(path.resolve(), timeout=0, holder_label="workflow.fire notification"):
            return update(fire._read_record(path) or record)
    except LockTimeout:
        return record
    except OSError as exc:
        return {**record, "notify_error": f"Notification refresh not persisted: {exc}"}
