"""workflow_fire.notify -- one-shot completion message from a finished fire to its ``notify_to`` roster identity.

``notify_terminal`` routes through ``agent_pipe.bridge`` (claude mailbox, codex/agy/grok legs).
Codex notices wait for persistent queue acceptance, never a remote turn; agy/grok hand off detached.
Trap: a ``handed-off`` Delivery means the leg started, not that the peer received the text; a later
``leg-failed`` lands in bridge.log.jsonl. ``notify_status`` records the Delivery status verbatim.
``queued`` means Codex accepted the notice in its inbox, not that its TUI consumed it.
Trap: the text reports driver exit, not workflow completion -- the headless driver exiting says nothing
about whether the run reached commit or halt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from coordinator_core.agent_pipe import bridge

FROM_IDENTITY = "workflow.fire"
_NON_TERMINAL = ("pending", "running")


def _text(record: dict) -> str:
    exit_code = record.get("exit_code")
    return (
        f"workflow.fire driver finished (driver-done, not run-completed): "
        f"fire_id={record.get('fire_id')} state={record.get('state')} "
        f"exit_code={'unknown' if exit_code is None else exit_code} "
        f"log={record.get('log_path') or 'unknown'}. "
        f"Check workflow.fire_status({record.get('fire_id')}) for the run outcome."
    )


def notify_terminal(record: dict) -> dict:
    """Send one bridge message to ``record['notify_to']``; return a copy with ``notified_at``/``notify_status`` set.

    No-op (record returned unchanged) when ``notify_to`` is absent, ``notified_at`` is already set, or the
    state is pending/running. ``refused``/``unroutable`` deliveries and any exception set ``notify_error``
    and leave ``notified_at`` unset. Never raises.
    """
    try:
        target = record.get("notify_to")
        if not target or record.get("notified_at") or record.get("state") in _NON_TERMINAL:
            return record
        out: dict[str, Any] = dict(record)
        delivery = bridge.route(bridge.make_message(FROM_IDENTITY, target, _text(record)))
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
