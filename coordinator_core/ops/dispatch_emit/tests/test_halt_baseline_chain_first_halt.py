"""chain_first_halt links fires of one sizing by the record's sizing_rel, never by run id."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import halt_baseline
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, RECORD_DIR_ROOT

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"
H1, H2, H3 = "1" * 40, "2" * 40, "3" * 40


def _record(root: Path, run: str, sizing: str = SIZING) -> None:
    d = root / RECORD_DIR_ROOT / run
    d.mkdir(parents=True)
    (d / "halted.json").write_text(
        json.dumps({"halted": "ledger-check", "sizing_rel": sizing, HALT_PLAN_FIELD: PLAN}), encoding="utf-8"
    )
    (d / "manifest.json").write_text("{}", encoding="utf-8")


def _state(*runs: tuple[str, bool, str]) -> SimpleNamespace:
    """``runs`` newest first, as ``(run_id, landed, halt_sha)``."""
    return SimpleNamespace(
        run_halts={r: (frozenset({"c"}) if landed else frozenset(), sha) for r, landed, sha in runs},
        run_first_halts={r: sha for r, _, sha in runs},
    )


def test_two_runs_of_one_sizing_resolve_the_older_halt(tmp_path):
    _record(tmp_path, "run-new")
    _record(tmp_path, "run-old")
    state = _state(("run-new", False, H2), ("run-old", False, H1))
    assert halt_baseline.chain_first_halt(tmp_path, PLAN, "run-new", state) == H1


def test_older_record_for_another_sizing_leaves_the_head_first_halt(tmp_path):
    _record(tmp_path, "run-new")
    _record(tmp_path, "run-old", sizing="state/sizings/other.yaml")
    state = _state(("run-new", False, H2), ("run-old", False, H1))
    assert halt_baseline.chain_first_halt(tmp_path, PLAN, "run-new", state) == H2


def test_walk_stops_after_the_first_halt_that_landed_a_chunk(tmp_path):
    for run in ("run-new", "run-mid", "run-old"):
        _record(tmp_path, run)
    state = _state(("run-new", False, H3), ("run-mid", True, H2), ("run-old", False, H1))
    assert halt_baseline.chain_first_halt(tmp_path, PLAN, "run-new", state) == H2


def test_manifestless_older_halt_still_links_but_is_not_carried(tmp_path):
    _record(tmp_path, "run-new")
    _record(tmp_path, "run-old")
    (tmp_path / RECORD_DIR_ROOT / "run-old" / "manifest.json").unlink()
    state = _state(("run-new", False, H2), ("run-old", False, H1))
    assert halt_baseline.chain_first_halt(tmp_path, PLAN, "run-new", state) == H1
    assert halt_baseline.walk_chain(tmp_path, PLAN, "run-new", state, SIZING) == ([], "run-old")


def test_since_halt_base_resolved_arm_walks_the_chain(tmp_path):
    _record(tmp_path, "run-new")
    _record(tmp_path, "run-old")
    state = _state(("run-new", False, H2), ("run-old", False, H1))
    assert halt_baseline.since_halt_base(tmp_path, {"run_id": "run-new", "plan": PLAN}, PLAN, None, state) == (
        H1, "resolved",
    )
