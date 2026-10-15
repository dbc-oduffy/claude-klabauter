"""Workstream-complete watcher stand-down: stop this session's coordinator watchers, prove it.

Attribution before match: a process is stopped only when a session key ties it to `session_id`
(a launch-store record the TaskStop attribution resolves to one orphan tree, or descent from the
session's claude anchor). A pattern match with no key is reported `unattributed` and never killed.
The kill return value is not the verdict; a second snapshot is.

NEGATIVE SPEC: no subprocess, no psutil, never sends the ended notice, never raises.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.bash_guards import _host_probe as _hp
from coordinator_core.bash_guards import _process_kill
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._session_census import _children, _is_claude, _subtree, session_census
from coordinator_core.bash_guards._taskstop_attribution import _depths, _stages, attribute
from coordinator_core.bash_guards._taskstop_kill import kill_tree_verified
from coordinator_core.group_em import box_notice, nomination, session_registry
from coordinator_core.workstream_complete import watcher_registry
from coordinator_core.workstream_complete.watcher_stand_down_contract import (
    ATTRIBUTION_ANCHOR_DESCENT,
    ATTRIBUTION_LAUNCH_RECORD,
    StandDown,
    StopRow,
)

# Seams: tests substitute a KillPrimitives and a snapshot callable.
_kill = _process_kill
_snapshot = _hp.snapshot

Key = Tuple[int, int]


def _record_pattern(command: str) -> Optional[watcher_registry.WatcherPattern]:
    for stage in _stages(command):
        pat = watcher_registry.match(" ".join(stage))
        if pat is not None:
            return pat
    return None


def _deepest_first(tree: Sequence[ProcRow]) -> List[ProcRow]:
    depth = _depths(tree)
    return sorted(tree, key=lambda p: depth[p.pid], reverse=True)


def _launch_record_trees(session_id: str, rows: Sequence[ProcRow]) -> Tuple[list, list]:
    """([(pattern, task_id, tree)], unattributed) for this session's launch records."""
    records = store.all_records()
    attributed, unattributed = [], []
    for rec in records:
        if rec.session_id != session_id:
            continue
        pat = _record_pattern(rec.command)
        if pat is None:
            continue
        tree, outcome, competing, candidates = attribute(rec, records, rows, _kill.command_line)
        if tree is None:
            unattributed.append({
                "watcher_id": pat.watcher_id, "task_id": rec.task_id,
                "reason": outcome.value, "competing": list(competing), "candidates": candidates,
            })
        else:
            attributed.append((pat, rec.task_id, tree))
    return attributed, unattributed


def _anchor_of(session_id: str, rows: Sequence[ProcRow], registry_dir: Optional[Path]) -> Optional[ProcRow]:
    """The registry-published pid, accepted only when the snapshot holds it under a claude image."""
    reg = session_registry.find_registry_row(session_id, registry_dir)
    if reg is None:
        return None
    for r in rows:
        if r.pid == reg.pid and _is_claude(r.name):
            return r
    return None


def _anchor_descent_trees(anchor: ProcRow, rows: Sequence[ProcRow], covered: Set[Key]) -> list:
    """[(pattern, tree)] of the topmost pattern-matching processes under the anchor's tool shells."""
    census = session_census(anchor, None, snapshot=rows)  # type: ignore[arg-type]
    if census is None:
        return []
    children = _children(rows)
    found, claimed = [], set(covered)
    for shell in census.shells:
        below = [p for p in _subtree(shell, children) if p.pid != shell.pid]
        depth = _depths(below)
        for p in sorted(below, key=lambda q: depth[q.pid]):
            if (p.pid, p.ctime) in claimed:
                continue
            text = _kill.command_line(p.pid, p.ctime)
            pat = watcher_registry.match(text) if text else None
            if pat is None:
                continue
            tree = _subtree(p, children)
            claimed.update((t.pid, t.ctime) for t in tree)
            found.append((pat, tree))
    return found


def _stand_down_holder(session_id: str, repo_root: str) -> Tuple[Optional[StandDown], Optional[dict]]:
    """Stand down when this session holds the role (per-repo record, else the box record)."""
    target: Optional[str] = None
    rec = nomination.read_record(repo_root)
    if rec is not None and str(rec.get("session_id") or "") == session_id:
        target = repo_root
    else:
        box = nomination.box_holder()
        if box is not None and str(box.get("session_id") or "") == session_id:
            target = str(box.get("repo_root") or repo_root)
    if target is None:
        return None, None
    res = nomination.stand_down(target, session_id)
    sd = StandDown(res.ok, res.exit_code, res.message)
    if not res.ok:
        return sd, {"recipients": [], "message": ""}
    reg = session_registry.find_registry_row(session_id)
    name = (reg.name if reg and reg.name else None) or (res.record or {}).get("peer_name")
    holder: Dict[str, Optional[str]] = {"sessionId": session_id, "name": name or None}
    recipients = box_notice.recipients_from_registry(session_id)
    return sd, {"recipients": recipients, "message": box_notice.render_notice(box_notice.NOTICE_ENDED, holder)}


def stand_down_watchers(
    session_id: str, repo_root: str, *, registry_dir: Optional[Path] = None
) -> Dict[str, Any]:
    """Stop `session_id`'s process-visible watchers and stand a holder down.

    Returns {stopped, survivors, unattributed, expires_on_its_own, stand_down, notice, warnings}.
    `survivors` is the re-snapshot verdict on every attributed process; an unreadable re-snapshot makes
    every attributed process a survivor (unverified) and adds a warning."""
    warnings: List[str] = []
    stopped: List[StopRow] = []
    survivors: List[Dict[str, object]] = []
    unattributed: List[Dict[str, object]] = []
    rows = _snapshot()
    if not rows:
        warnings.append("process table unreadable; no watcher could be attributed")
        rows = []

    plan: list = []  # (pattern, attribution, task_id, tree)
    covered: Set[Key] = set()
    if rows:
        try:
            attributed, unattributed = _launch_record_trees(session_id, rows)
            for pat, task_id, tree in attributed:
                plan.append((pat, ATTRIBUTION_LAUNCH_RECORD, task_id, tree))
                covered.update((p.pid, p.ctime) for p in tree)
        except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
            warnings.append(f"launch-record attribution failed: {type(exc).__name__}: {exc}")
        try:
            anchor = _anchor_of(session_id, rows, registry_dir)
            if anchor is not None:
                for pat, tree in _anchor_descent_trees(anchor, rows, covered):
                    plan.append((pat, ATTRIBUTION_ANCHOR_DESCENT, None, tree))
        except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
            warnings.append(f"anchor descent failed: {type(exc).__name__}: {exc}")

    attributed_keys: Dict[Key, Tuple[str, str]] = {}
    for pat, attribution, task_id, tree in plan:
        for p in tree:
            attributed_keys[(p.pid, p.ctime)] = (pat.watcher_id, p.name)
        killed, complete, reason = kill_tree_verified(_deepest_first(tree), _snapshot, _kill)
        if reason:
            warnings.append(f"{pat.watcher_id}: {reason}")
        stopped.append(StopRow(pat.watcher_id, attribution, tuple(killed), task_id))
        if task_id and complete:
            store.delete_record(task_id)

    if plan:
        again = _snapshot()
        live = {(r.pid, r.ctime) for r in again} if again else None
        if live is None:
            warnings.append("re-snapshot unreadable; no attributed process is verified dead")
        for key, (watcher_id, name) in attributed_keys.items():
            alive = key in live if live is not None else True
            if alive:
                survivors.append({"watcher_id": watcher_id, "pid": key[0], "ctime": key[1], "name": name})

    stand_down: Optional[StandDown] = None
    notice: Optional[dict] = None
    try:
        stand_down, notice = _stand_down_holder(session_id, repo_root)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        warnings.append(f"stand-down failed: {type(exc).__name__}: {exc}")

    return {
        "stopped": [asdict(s) for s in stopped],
        "survivors": survivors,
        "unattributed": unattributed,
        "stand_down": asdict(stand_down) if stand_down else None,
        "notice": notice,
        "expires_on_its_own": list(watcher_registry.expires_on_its_own()),
        "warnings": warnings,
    }


__all__ = ["stand_down_watchers"]
