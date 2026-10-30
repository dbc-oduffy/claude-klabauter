"""A ledger row that is open, rulable and whose `remaining` names a path inside the run's write set runs the remnant leg and the re-verify before any ledger-check halt; a vague or out-of-set gap still halts."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ledger_recovery import REMNANT_LABEL, REMNANT_REVERIFY_LABEL
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_recovery_loop import (
    _DISK,
    _MET,
    _labels,
    _run,
    _script,
    _unmet,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_remnant_completion import _record
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_IN_SET = "scripts/census.py"
_MANIFEST = {**_DISK, "rulable_ids": ["R1"], "doc_writes": [_IN_SET]}
_CONCRETE = _unmet([f"Change the predicate in {_IN_SET} to credit qualified names, add a test, retake the receipt"])
_DONE = {"status": "DONE", "paths": [_IN_SET], "reason": "completed"}


def test_a_rulable_row_naming_a_write_set_path_runs_the_remnant_leg_and_reverify_then_does_not_halt(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_CONCRETE], manifest=_MANIFEST,
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL) < labels.index("commit")
    assert out["result"]["halted_by"] is None
    rec = _record(out)
    assert rec["status"] == "fixed" and rec["items"] == [{"kind": "ledger", "id": "R1"}]
    assert _IN_SET in rec["footprint"]


def test_a_gap_the_reverify_still_finds_open_halts_on_ledger_check_after_one_remnant_pass(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_CONCRETE], manifest=_MANIFEST,
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_CONCRETE]}},
    )
    labels = _labels(out)
    assert labels.count(REMNANT_LABEL) == 1 and labels.count(REMNANT_REVERIFY_LABEL) == 1
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


def test_a_rulable_row_naming_a_path_outside_the_write_set_halts_with_no_remnant_leg(monkeypatch):
    outside = _unmet(["Change the predicate in elsewhere/other.py to credit names"])
    out = _run(_script(monkeypatch), items=[outside], manifest=_MANIFEST)
    assert REMNANT_LABEL not in _labels(out)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


def test_a_rulable_row_with_no_concrete_path_halts_with_no_remnant_leg(monkeypatch):
    out = _run(_script(monkeypatch), items=[_unmet(["something no chunk owns"])], manifest=_MANIFEST)
    assert REMNANT_LABEL not in _labels(out)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


@pytest.mark.parametrize(
    "extra",
    [{"writes": [_IN_SET]}, {"writes_under": ["scripts/"]}],
    ids=["rows-writes", "rows-writes-under"],
)
def test_a_path_named_by_a_manifest_row_write_set_runs_the_remnant_leg(monkeypatch, extra):
    manifest = {**_DISK, "rulable_ids": ["R1"], "rows": [*_DISK["rows"], {**_DISK["rows"][0], "id": "C3", **extra}]}
    out = _run(
        _script(monkeypatch), items=[_CONCRETE], manifest=manifest,
        replies={REMNANT_LABEL: _DONE, REMNANT_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert labels.index(REMNANT_LABEL) < labels.index(REMNANT_REVERIFY_LABEL)
    assert out["result"]["halted_by"] is None


def test_a_path_outside_a_manifest_row_writes_under_prefix_halts(monkeypatch):
    manifest = {**_DISK, "rulable_ids": ["R1"], "rows": [*_DISK["rows"], {**_DISK["rows"][0], "id": "C3", "writes_under": ["docs/"]}]}
    out = _run(_script(monkeypatch), items=[_CONCRETE], manifest=manifest)
    assert REMNANT_LABEL not in _labels(out)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
