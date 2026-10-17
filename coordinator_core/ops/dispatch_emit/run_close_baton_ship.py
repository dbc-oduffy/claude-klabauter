"""
coordinator_core.ops.dispatch_emit.run_close_baton_ship -- the inline ``baton-ship`` close leg.

Purpose: for each baton the run claims, write the ship stamp (``deployment_state: shipped``,
``shipped_in``) and append one Session Ledger row, as file writes with no git and no process
spawn, so the baton files can ride the coded-stamp commit's ``also_commit``.

Negative-spec:
  - ``sha`` is the landed work commit, never the stamp commit (a file cannot name the commit
    that contains it).
  - Calls the verbs' sync text forms (``stamp_apply``, ``ship_apply``, ``append_apply``)
    directly, never their CLIs or IPC; the owed verbs compose inside one ``locked_rmw`` write.
  - A baton already stamped ``shipped_in`` this sha with this run's ledger row is
    ``discharged``; a rerun appends no second ledger row.
  - The ledger row's summary is the fixed ``run <run_id> shipped in <sha8>`` text: it names the
    run, it is not judge prose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.dispatch_emit.run_close import LegResult

_CEREMONY = "run-close"


def _marker(run_id: str, sha: str) -> str:
    return f"run {run_id} shipped in {sha[:8]}"


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


def _ship_baton(root: Path, rel: str, run_id: str, sha: str, text: str) -> Optional[str]:
    """Apply the owed stamp / ship / ledger-row verbs in one locked write; return the refusal text."""
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    need_stamp = not _stamped(text, sha)
    need_row = not _has_ledger_row(text, run_id, sha)
    if not (need_stamp or need_row):
        return None
    stage = ["stamp"]

    def compose(old: str) -> str:
        from coordinator_core.ops.handoff_append_session_ledger import append_apply
        from coordinator_core.ops.handoff_stamp import stamp_apply
        from coordinator_core.ops.handoff_transition import ship_apply

        new = old
        if need_stamp:
            stage[0] = "stamp"
            new = stamp_apply(
                new, {"handoff_path": rel, "sha": sha[:8], "kind": "ship-commit"}, repo_root=root
            )
            stage[0] = "ship"
            new = ship_apply(new, {"handoff_path": rel, "verb": "ship"}, repo_root=root)
        if need_row:
            stage[0] = "ledger row"
            new = append_apply(
                new,
                {"handoff_path": rel, "summary": _marker(run_id, sha), "closing_ceremony": _CEREMONY},
                repo_root=root,
            )
        return new

    try:
        locked_rmw(root / rel, compose, repo_root=root)
    except MutateAbort as exc:
        reply = exc.args[0] if exc.args else None
        error = reply.get("error") if isinstance(reply, dict) else reply
        return f"{stage[0]} refused: {error}"
    except (LockTimeout, OSError) as exc:
        return f"{stage[0]} refused: {exc}"
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
        if _stamped(text, sha) and _has_ledger_row(text, run_id, sha):
            continue
        error = _ship_baton(root, rel, run_id, sha, text)
        if error is not None:
            return LegResult("failed", f"{rel}: {error}")
        wrote = True
    names = ", ".join(claimed_batons)
    if not wrote:
        return LegResult("discharged", f"already shipped at {sha[:8]}: {names}")
    return LegResult("ran", f"shipped at {sha[:8]}: {names}")
