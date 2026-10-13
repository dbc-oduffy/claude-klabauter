"""
coordinator_core.ops.dispatch_emit.run_close_baton_ship -- the inline ``baton-ship`` close leg.

Purpose: for each baton the run claims, write the ship stamp (``deployment_state: shipped``,
``shipped_in``) and append one Session Ledger row, as file writes with no git and no process
spawn, so the baton files can ride the coded-stamp commit's ``also_commit``.

Negative-spec:
  - ``sha`` is the landed work commit, never the stamp commit (a file cannot name the commit
    that contains it).
  - Calls the ``handoff.stamp``, ``handoff.transition`` and ``handoff.append_session_ledger``
    handlers directly, never their CLIs.
  - A baton already stamped ``shipped_in`` this sha with this run's ledger row is
    ``discharged``; a rerun appends no second ledger row.
  - The ledger row's summary is the fixed ``run <run_id> shipped in <sha8>`` text: it names the
    run, it is not judge prose.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Awaitable, Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.dispatch_emit.run_close import LegResult

_CEREMONY = "run-close"


def _marker(run_id: str, sha: str) -> str:
    return f"run {run_id} shipped in {sha[:8]}"


def _run(coro: Awaitable[Any]) -> Any:
    """Run *coro* to completion from sync code, on a helper thread when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _read(root: Path, rel: str) -> Optional[str]:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _stamped(text: str, sha: str) -> bool:
    split = split_frontmatter(text)
    if split is None:
        return False
    shipped_in = read_fm_field_unquoted(split.fm_text, "shipped_in")
    state = read_fm_field_unquoted(split.fm_text, "deployment_state")
    return bool(shipped_in) and state == "shipped" and sha.startswith(shipped_in[:8])


def _has_ledger_row(text: str, run_id: str, sha: str) -> bool:
    marker = _marker(run_id, sha)
    in_ledger = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_ledger = line.strip().lower().startswith("## session ledger")
            continue
        if in_ledger and marker in line:
            return True
    return False


def _stamp(root: Path, rel: str, sha: str) -> Optional[str]:
    from coordinator_core.ops.handoff_stamp import _handler as stamp_handler
    from coordinator_core.ops.handoff_transition import _handler as transition_handler

    stamped = _run(stamp_handler({"handoff_path": rel, "sha": sha[:8], "kind": "ship-commit"}, repo_root=root))
    if stamped.get("exit_code") != 0:
        return f"stamp refused: {stamped.get('error')}"
    shipped = _run(transition_handler({"handoff_path": rel, "verb": "ship"}, repo_root=root))
    if shipped.get("exit_code") != 0:
        return f"ship refused: {shipped.get('error')}"
    return None


def _append_row(root: Path, rel: str, run_id: str, sha: str) -> Optional[str]:
    from coordinator_core.ops.handoff_append_session_ledger import _handler as ledger_handler

    reply = _run(
        ledger_handler(
            {"handoff_path": rel, "summary": _marker(run_id, sha), "closing_ceremony": _CEREMONY},
            repo_root=root,
        )
    )
    if reply.get("exit_code") != 0:
        return f"ledger row refused: {reply.get('error')}"
    return None


def baton_ship_leg(
    root: Path, run_id: str, claimed_batons: Sequence[str], sha: str
) -> LegResult:
    """Stamp each claimed baton shipped at *sha* and append its Session Ledger row.

    ``not-applicable`` with no claimed baton; ``discharged`` when every baton already carries
    this sha's stamp and this run's ledger row; ``failed`` naming the first baton that refused;
    otherwise ``ran``. Reruns write nothing for a baton already done.
    """
    if not claimed_batons:
        return LegResult("not-applicable", "run claims no baton")
    if not sha:
        return LegResult("failed", "no landed work commit to stamp shipped_in with")
    wrote = False
    for rel in claimed_batons:
        text = _read(root, rel)
        if text is None:
            return LegResult("failed", f"{rel}: baton not readable")
        if not _stamped(text, sha):
            error = _stamp(root, rel, sha)
            if error is not None:
                return LegResult("failed", f"{rel}: {error}")
            wrote = True
            text = _read(root, rel) or ""
        if not _has_ledger_row(text, run_id, sha):
            error = _append_row(root, rel, run_id, sha)
            if error is not None:
                return LegResult("failed", f"{rel}: {error}")
            wrote = True
    names = ", ".join(claimed_batons)
    if not wrote:
        return LegResult("discharged", f"already shipped at {sha[:8]}: {names}")
    return LegResult("ran", f"shipped at {sha[:8]}: {names}")
