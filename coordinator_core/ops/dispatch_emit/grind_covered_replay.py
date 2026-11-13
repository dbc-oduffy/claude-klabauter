"""Replay probe: what an already-covered row costs a lessons grind, in agent calls.

Drives ``grind_compose.run_admission`` over one 9-row batch with stub agents and prints JSON
``{agent_calls_total, agent_calls_per_covered_row, covered_row_verify_calls, covered_row_handbacks,
agent_calls_by_kind}``. Read-only: writes no file. ``python -m coordinator_core.ops.dispatch_emit.grind_covered_replay``.

Fixture: triage returns the profile's fix verdict for every row. r0 and r1 are found covered at the
pre-fix routing verify; r2 reaches fix, which reports ``covered_by`` and no edits, and its post-fix verify
fails with ``covered_by``; r3..r8 pass every stage. The stubs answer by the row's current node, read off the
engine's own stack frame, so the fixture is identical whatever routing the engine picks for a covered row.
A verifier at a discard node passes (discarding a covered row is correct); at any other pre-fix node it
reports the row covered. ``agent_calls_per_covered_row`` counts only calls whose unit is a covered row
(fix, verify), never the batch-shared triage, refute-close, commit, adjudicate or sweep calls.
The post-fix covered close issues one undo-only verify call, so ``covered_row_verify_calls`` is not zero after
the closing verify; rows at a refute-close node wait for the batch, so one close call settles every covered row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp

PROFILE = "lessons"
TRIAGE_NODE = "triage"
COVERED_BY = "docs/wiki/x.md:1"
ROW_COUNT = 9
PRE_FIX_COVERED = ("r0", "r1")
FIX_COVERED = ("r2",)
COVERED_ROWS = PRE_FIX_COVERED + FIX_COVERED
_DISCARD_VERDICT = "discard"


class _Budget:
    total = None
    _spent = 0

    def spent(self) -> int:
        return self._spent

    def remaining(self) -> None:
        return None

    def spend(self, n: int) -> None:
        self._spent += n


def _engine_local(name: str) -> Any:
    """``name`` from the nearest engine frame above the stub: ``row`` in a row stage, ``proposal_ids`` in the close.
    Trap: couples to those local names in ``run_admission``; a rename there silently yields None."""
    frame = sys._getframe(2)
    while frame is not None:
        if name in frame.f_locals:
            return frame.f_locals[name]
        frame = frame.f_back
    return None


def _verify_reply(row_id: str, node: Optional[str], routing: Mapping[str, dict]) -> dict:
    if row_id in PRE_FIX_COVERED and node in routing and not _is_discard(routing, node):
        return {"outcome": "fail", "reason": "covered", "covered_by": COVERED_BY}
    if row_id in FIX_COVERED and node in routing and routing[node]["kind"] == "verify" and node not in gc.pre_fix_verify_nodes(routing):
        return {"outcome": "fail", "reason": "covered", "covered_by": COVERED_BY, "undo": {"outcome": "undone"}}
    return {"outcome": "pass"}


def _is_discard(routing: Mapping[str, dict], node: str) -> bool:
    """True for the verify node the triage ``discard`` verdict enters."""
    return routing[TRIAGE_NODE]["edges"].get(_DISCARD_VERDICT) == node


def _fix_verdict(routing: Mapping[str, dict]) -> str:
    entries = gc.triage_fix_entries(routing, TRIAGE_NODE)
    return sorted(entries)[0]


def replay(routing: Mapping[str, dict]) -> dict:
    """Run the 9-row fixture through ``run_admission`` over ``routing`` and return the report dict."""
    row_ids = [f"r{i}" for i in range(ROW_COUNT)]
    verdict = _fix_verdict(routing)
    per_row_calls: dict[str, list[str]] = {rid: [] for rid in row_ids}

    def agent(kind: str, unit: str) -> Any:
        if unit in per_row_calls:
            per_row_calls[unit].append(kind)
        if kind == "triage":
            return [{"row": rid, "verdict": verdict, "tshirt_size": "S", "tradeoff": ""} for rid in row_ids]
        if kind == "refute-close":
            proposals = _engine_local("proposal_ids") or []
            return {"confirmed": [{"row": rid, "new_path": f"archive/{rid}"} for rid in proposals]}
        if kind == "fix":
            if unit in FIX_COVERED:
                return {"outcome": "done", "touched_files": [], "covered_by": COVERED_BY,
                        "has_tradeoff": False, "ruling_on_tradeoff": "no-ruling"}
            return {"outcome": "done", "touched_files": [f"src/{unit}.py"],
                    "has_tradeoff": False, "ruling_on_tradeoff": "no-ruling"}
        if kind == "verify":
            row = _engine_local("row")
            return _verify_reply(unit, getattr(row, "node", None), routing)
        if kind == "commit":
            return {"committed": True, "sha": "0" * 40}
        if kind == "ledger-sweep":
            return {"outcome": "recorded", "swept": True}
        return {}

    result = gc.run_admission(
        [("b0", row_ids)], routing, TRIAGE_NODE, reserve=gc.batch_reserve(ROW_COUNT),
        max_agent_calls=None, budget=_Budget(), agent=agent,
    )
    total = result["spend"]["agent_calls_total"]
    covered_calls = sum(
        1 for rid in COVERED_ROWS for kind in per_row_calls[rid] if kind in ("fix", "verify")
    )
    return {
        "agent_calls_total": total,
        "agent_calls_per_covered_row": round(covered_calls / len(COVERED_ROWS), 2),
        "covered_row_verify_calls": {rid: per_row_calls[rid].count("verify") for rid in COVERED_ROWS},
        "covered_row_handbacks": {
            rid: [h["type"] for h in result["handed_back"] if h["row"] == rid] for rid in COVERED_ROWS
        },
        "agent_calls_by_kind": dict(result["spend"]["agent_calls_by_stage_kind"]),
    }


def lessons_routing() -> dict[str, dict]:
    """The lessons profile's routing table, loaded through the content-root resolver."""
    from coordinator_core.resolve_coordinator_clone import resolve_content_root

    profile_dir = Path(resolve_content_root()) / "queue-profiles"
    profile = gp.load_profile(PROFILE, profile_dir)
    gp.validate_graph(profile)
    return gc.build_routing_table(profile)


def main() -> int:
    print(json.dumps(replay(lessons_routing()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
