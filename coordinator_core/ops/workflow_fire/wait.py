"""
coordinator_core.ops.workflow_fire.wait -- JSON-RPC "workflow.fire_wait".

Purpose: ONE non-blocking read answering "has this fire exited, and what did it
land". It refreshes the record through ``fire.fire_status`` and never sleeps; the
blocking loop lives in the cold ``coordinator-invoke`` client, so no warm handler
is held past ``ipc._timeout_for``.

Wire params:
    fire_id (str, required)           -- the ``fire_id`` from a ``workflow.fire`` reply.
    poll_interval_s (number, optional) -- read by the client loop only (default 5,
                                          clamped 1..60); accepted and ignored here.

Reply (always these keys): ``fire_id``, ``found``, ``state``, ``outcome``, ``run_id``,
``commit_sha``, ``commit_sha_reason``, ``timed_out``, ``exit_code``, ``timeout_s``,
``deadline_at``, ``reason`` (why an exited fire failed, else None).

Negative-spec:
  - Never waits: a running fire replies ``state: running`` with no git spawn.
  - ``commit_sha`` is resolved only once ``state`` is ``exited``: the record's landing
    sha first, else one ``git log --all --grep "Session-Id: <driver_session_id>"``.
    A zero there is a question, never a verdict (``fire.fire_status`` docstring).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops.workflow_fire import fire

DEFAULT_TIMEOUT_S = 14400


def _deadline(record: dict) -> tuple[Optional[float], Optional[float]]:
    timeout_s = record.get("timeout_s")
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
        timeout_s = DEFAULT_TIMEOUT_S
    start = record.get("started_at") or record.get("reserved_at")
    if not isinstance(start, (int, float)):
        return timeout_s, None
    return timeout_s, start + timeout_s


def _commit_sha(record: dict, cwd: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    landing = record.get("landing")
    if isinstance(landing, dict) and landing.get("sha") and landing.get("committed") is not False:
        return str(landing["sha"]), None
    session_id = record.get("driver_session_id")
    if not session_id:
        return None, "no landing sha and no driver session id"
    done = run_git(
        ["log", "--all", "--fixed-strings", "--grep", f"Session-Id: {session_id}", "-n1", "--format=%H"],
        cwd=cwd,
    )
    sha = done.stdout.strip() if done.returncode == 0 else ""
    if sha:
        return sha, None
    return None, "nothing landed" if done.returncode == 0 else "git log failed"


def _failure_reason(record: dict) -> Optional[str]:
    """Why an exited fire failed, from the record's own evidence; None when it did not fail.

    Failed means a nonzero ``exit_code`` or a non-clean settled ``outcome``. The parts are
    the outcome, its failure subtype and basis, the execution note, then the last startup-log
    line, so a nonzero exit never reaches the caller as a bare number.
    """
    if record.get("state") != "exited":
        return None
    code = record.get("exit_code")
    outcome = record.get("outcome")
    failed_code = isinstance(code, int) and not isinstance(code, bool) and code != 0
    if not failed_code and outcome not in ("failed", "denied", "truncated", "not_landed"):
        return None
    parts = []
    if failed_code:
        parts.append(f"workflow exit_code={code}")
    if outcome:
        parts.append(f"outcome={outcome}")
    for key in ("failure_subtype", "outcome_basis", "execution_note"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    tail = record.get("startup_log_tail")
    if isinstance(tail, str):
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if lines:
            parts.append(f"log tail: {lines[-1]}")
    return "; ".join(parts) if parts else "workflow failed; the fire record carries no reason"


@register_op("workflow.fire_wait")
def _workflow_fire_wait(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.fire_wait" handler -- see module docstring for the wire contract.

    Raises:
        ValueError -- if ``fire_id`` is missing.
    """
    fire_id = params.get("fire_id")
    if not fire_id:
        raise ValueError("workflow.fire_wait requires param: fire_id")

    cwd = str(repo_root) if repo_root is not None else None
    record = fire.fire_status(fire_id, cwd=cwd)
    if record is None:
        return {
            "fire_id": fire_id, "found": False, "state": None, "outcome": None, "run_id": None,
            "commit_sha": None, "commit_sha_reason": "no record for fire_id", "timed_out": False,
            "exit_code": None, "timeout_s": None, "deadline_at": None,
            "reason": "no record for fire_id",
        }

    state = record.get("state")
    timeout_s, deadline_at = _deadline(record)
    sha, reason = (None, "run not exited")
    if state == "exited":
        sha, reason = _commit_sha(record, str(repo_root) if repo_root is not None else record.get("repo_root"))
    return {
        "reason": _failure_reason(record),
        "fire_id": fire_id,
        "found": True,
        "state": state,
        "outcome": record.get("outcome"),
        "run_id": record.get("run_id"),
        "commit_sha": sha,
        "commit_sha_reason": reason,
        "timed_out": state != "exited" and deadline_at is not None and time.time() > deadline_at,
        "exit_code": record.get("exit_code"),
        "timeout_s": timeout_s,
        "deadline_at": deadline_at,
    }
