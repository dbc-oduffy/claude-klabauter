"""The base a ledger resume's since-halt check measures product changes from.

A re-fire writes its own halt commit before the check, so the newest halt is never the base: the
base is the first ledger-check halt of the sizing-linked chain of runs. The halt walk is bounded by
``git log -n 500``, so "first" means the oldest halt visible in that window.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

_SHA40 = re.compile(r"[0-9a-f]{40}")


def _sha(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else None


def first_halt_sha(state: Any, run_id: Optional[str]) -> Optional[str]:
    """The oldest visible ledger-check halt sha of ``run_id`` in ``state`` (a ResumeLanded), else None."""
    if not isinstance(run_id, str):
        return None
    return _sha(getattr(state, "run_first_halts", {}).get(run_id))


def _halt_link(root: Path, run: str, sha: Optional[str]) -> tuple:
    """``(record, sizing_rel, plan_rel)`` of ``run``'s ledger-check halt; ``record`` is None when
    only ``halted.json`` is readable (no manifest), and the whole tuple is ``(None, None, None)``
    when no ledger-check halt record is readable at all."""
    from coordinator_core.ops.dispatch_emit import ask_contract as ac

    record = ac.read_halt_record(root, run, at_sha=sha)
    if record is not None:
        return record, record.sizing_rel, record.plan_rel
    try:
        halted = ac._read_json_object(ac.record_dir(root, run) / "halted.json")
    except ValueError:
        return None, None, None
    if (halted is None or halted.get("halted") != "ledger-check") and isinstance(sha, str) and _SHA40.fullmatch(sha):
        halted = ac._read_json_at_commit(Path(root), sha, f"{ac.RECORD_DIR_ROOT}/{run}/halted.json")
    if halted is None or halted.get("halted") != "ledger-check":
        return None, None, None
    sizing, plan = halted.get("sizing_rel"), halted.get(ac.HALT_PLAN_FIELD)
    if not isinstance(sizing, str) or not sizing:
        return None, None, None
    return None, sizing, plan if isinstance(plan, str) and plan else None


def walk_chain(
    root: Path, plan_rel: Optional[str], run_id: str, state: Any, sizing_rel: Optional[str],
) -> tuple[list, str]:
    """``(earlier, oldest)``: the ``(record, landed)`` pairs of runs older than ``run_id`` in
    ``state.run_halts`` that share ``sizing_rel`` and ``plan_rel``, and the oldest run linked. Stops
    at a halt for another sizing or plan, an unreadable ``halted.json``, and after the first halt
    that landed a chunk. Runs link by the sizing the record names, never by run id. ``earlier``
    additionally stops at a halt with no readable manifest (its record cannot be carried), while
    ``oldest`` walks on. A head run that itself landed a chunk walks nothing."""
    chain = [(run, bool(landed), sha) for run, (landed, sha) in state.run_halts.items()]
    runs = [entry[0] for entry in chain]
    after = chain[runs.index(run_id) + 1 :] if run_id in runs else [e for e in chain if e[0] != run_id]
    earlier: list = []
    oldest = run_id
    if any(e[0] == run_id and e[1] for e in chain):
        return earlier, oldest
    carrying = True
    for run, lands, sha in after:
        record, sizing, plan = _halt_link(root, run, sha)
        if sizing is None or sizing != sizing_rel or (plan is not None and plan != plan_rel):
            break
        carrying = carrying and record is not None
        if carrying:
            earlier.append((record, lands))
        oldest = run
        if lands:
            break
    return earlier, oldest


def chain_first_halt(root: Path, plan_rel: Optional[str], run_id: str, state: Any) -> Optional[str]:
    """The first halt of the sizing-linked chain ending at ``run_id`` (see ``walk_chain``); the
    run's own first halt when its record is unreadable."""
    head = getattr(state, "run_halts", {}).get(run_id)
    _, sizing, _ = _halt_link(root, run_id, head[1] if head else None)
    if sizing is None:
        return first_halt_sha(state, run_id)
    _, oldest = walk_chain(root, plan_rel, run_id, state, sizing)
    return first_halt_sha(state, oldest)


def since_halt_base(
    root: Path, resume: Mapping, plan_rel: Optional[str], stage_head: Optional[str], state: Any = None,
) -> tuple[Optional[str], Optional[str]]:
    """``(base, source)``: the ``since_sha`` param (``param``), else the resume run's chain first
    halt (``resolved``; one ``resume_landed_state`` call, only when ``state`` is None), else the
    legacy ``halt_sha`` param (``param``), else ``stage_head`` (``stage-head``), else
    ``(None, None)``."""
    base = _sha(resume.get("since_sha"))
    if base is not None:
        return base, "param"
    run_id, plan = resume.get("run_id"), resume.get("plan") or plan_rel
    if isinstance(run_id, str) and isinstance(plan, str) and plan:
        if state is None:
            from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

            state = resume_landed_state(root, plan)
        base = chain_first_halt(root, plan, run_id, state)
        if base is not None:
            return base, "resolved"
    base = _sha(resume.get("halt_sha"))
    if base is not None:
        return base, "param"
    if stage_head:
        return stage_head, "stage-head"
    return None, None
