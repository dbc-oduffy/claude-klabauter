"""Phase ledger: the ask pipeline's phases, their dependency DAG, and what a halt invalidates.

A resume re-runs the halted phase and its transitive dependents (plus phases whose inputs a
`workflow.revise` changed) and reuses every other phase that left a durable artifact. Leaf module:
no subprocess, no import from ask_compose or terminal_commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Optional

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

PHASES = (
    "gate",
    "size",
    "plan",
    "plan_review",
    "plan_gate_rulings",
    "execute",
    "review",
    "test",
    "criterion",
    "commit",
)

#: phase -> the phases it consumes. A change to a phase invalidates every transitive dependent.
DEPENDS = {
    "gate": (),
    "size": ("gate",),
    "plan": ("size",),
    "plan_review": ("plan",),
    "plan_gate_rulings": ("plan_review",),
    "execute": ("plan_gate_rulings",),
    "review": ("execute",),
    "test": ("review",),
    "criterion": ("test",),
    "commit": ("criterion",),
}

#: halt kind -> the phase that halted. An unlisted kind invalidates every phase.
HALT_PHASE = {
    "review_blocked": "review",
    "footprint_gap": "execute",
    "plan_gate": "plan_gate_rulings",
    "plan_gate_ruling": "plan_gate_rulings",
    "ruling": "plan_gate_rulings",
    "rulings": "plan_gate_rulings",
}

#: A revise-changed input -> the first phase that consumes it. An unlisted input invalidates every phase.
REVISED_INPUT_PHASE = {
    "sizing": "size",
    "plan": "plan_review",
    "rulings": "plan_gate_rulings",
    "requirement": "criterion",
}

#: Gate verdict field a resume carries: {reuse: [phase], rerun: [phase], verdicts: {phase: obj}}.
RESUME_PHASES_FIELD = "resume_phases"

KEPT_FILE = "kept.json"


def _dependents(phase: str) -> set:
    out = {phase}
    grew = True
    while grew:
        grew = False
        for p, deps in DEPENDS.items():
            if p not in out and out.intersection(deps):
                out.add(p)
                grew = True
    return out


def invalidated(halt_kind: Optional[str], revised: Iterable[str] = ()) -> frozenset:
    """The halted phase, its transitive dependents, and the phases a revise's changed inputs feed.

    `halt_kind` None means no halt (revise-only). An unknown halt kind or revised input returns
    every phase: a safe full pass."""
    out: set = set()
    if halt_kind is not None:
        phase = HALT_PHASE.get(halt_kind)
        if phase is None:
            return frozenset(PHASES)
        out |= _dependents(phase)
    for item in revised or ():
        phase = REVISED_INPUT_PHASE.get(item)
        if phase is None:
            return frozenset(PHASES)
        out |= _dependents(phase)
    return frozenset(out)


def _run_dirs(repo_root: Path, run_id: str) -> list:
    if not isinstance(run_id, str) or not safe_id(run_id):
        return []
    root = Path(repo_root)
    dirs = []
    scratch = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if scratch is not None:
        dirs.append(scratch)
    try:
        dirs.append(record_dir(root, run_id))
    except ValueError:
        pass
    return dirs


def derive(repo_root: Path, run_id: str) -> dict:
    """`{phase: verdict}` for each phase the predecessor run's `kept.json` proves landed.

    plan and plan_review come from a recorded plan gate, execute from kept chunks. A phase with no
    durable artifact (gate, size, plan_gate_rulings, review, test, criterion, commit) is absent: it
    always re-runs. An unsafe run_id, a missing or malformed file reads `{}`."""
    for run_dir in _run_dirs(repo_root, run_id):
        try:
            data = json.loads((run_dir / KEPT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out: dict = {}
        gate = data.get("plan_gate")
        if isinstance(gate, dict) and gate.get("plan"):
            out["plan"] = {"plan": gate["plan"], "plan_sha256": gate.get("plan_sha256")}
            if gate.get("reviews") is not None:
                out["plan_review"] = {"plan": gate["plan"], "reviews": gate["reviews"], "coverage": gate.get("coverage")}
        chunks = data.get("chunks")
        if isinstance(chunks, dict) and chunks:
            out["execute"] = {"chunks": sorted(chunks)}
        return out
    return {}


def resume_phases(
    landed: Mapping, halt_kind: Optional[str], revised: Iterable[str] = ()
) -> dict:
    """The gate's `resume_phases` value: landed phases the halt left valid are reused with their verdicts."""
    stale = invalidated(halt_kind, revised)
    reuse = [p for p in PHASES if p in landed and p not in stale]
    return {
        "reuse": reuse,
        "rerun": [p for p in PHASES if p not in reuse],
        "verdicts": {p: landed[p] for p in reuse},
    }
