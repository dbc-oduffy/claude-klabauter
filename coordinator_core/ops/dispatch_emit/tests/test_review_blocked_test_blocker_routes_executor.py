"""A review_blocked halt over a failing repo test names the executor fix leg, not a review re-run."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.halted_record_commit import _ledger_halt_plan_and_blocking
from coordinator_core.ops.review_test_blocker import TEST_BLOCKER
from coordinator_core.win_portability import no_console_creationflags

_SIZING = "state/sizings/x.yaml"
_PLAN = "docs/plans/p.md"
_NODE = "coordinator_core/tests/test_x.py::test_tripwire"


def _run(wave, reverify) -> dict | None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"let _halted = null; const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _planRel = {_PLAN!r};\n"
        f"const _manifest = {{ ledger_item_ids: ['R1'] }}; const _reviewWave = {json.dumps(wave)};\n"
        f"const _reviewReverify = {json.dumps(reverify)};\n"
        + ask_compose._refire_next_action_helper_js(_SIZING)
        + "\n"
        + ask_compose._review_blocked_halt_js(_SIZING)
        + "\nconsole.log(JSON.stringify(_halted));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _reverify(**kw) -> dict:
    base = {
        "failing": [_NODE, "a.py::old", "a.py::kept"],
        "pre_existing": ["a.py::old"],
        "kept": ["a.py::kept"],
        "failing_detail": [{"node_id": _NODE, "message": "AssertionError: tree walk\nsecond line"}],
    }
    base.update(kw)
    return base


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_failing_test_blocker_halts_naming_the_executor_fix_leg():
    halted = _run([], _reverify())

    assert halted["halted"] == ask_compose.HALT_REVIEW_BLOCKED
    assert halted["blocker"] == TEST_BLOCKER
    assert halted["failing"] == [{"node_id": _NODE, "message": "AssertionError: tree walk\nsecond line"}]
    assert halted["gaps"] == [f"{_NODE}: AssertionError: tree walk"]
    assert halted["plan"] == _PLAN
    assert halted["items"] == [
        {"id": "R1", "remaining": halted["gaps"], "evidence": halted["gaps"][0]}
    ]
    assert "executor fix leg against these failing tests" in json.dumps(halted["next_action"])
    assert halted["gaps"][0] in json.dumps(halted["next_action"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_halt_record_reads_plan_and_items_for_the_ledger_pointer(tmp_path):
    halted = _run([], _reverify())
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / _PLAN).write_text("x")

    plan, ids, items = _ledger_halt_plan_and_blocking(tmp_path, halted)

    assert plan == _PLAN
    assert ids == ("R1",)
    assert items[0]["remaining"] == halted["gaps"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_blocked_slices_ride_along_with_the_test_blocker():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0, "sidecar_path": "s1.md"}]
    halted = _run(wave, _reverify())

    assert halted["blocker"] == TEST_BLOCKER
    assert "unapplied_findings" not in halted
    assert halted["slices"] == [{"slice_id": "s1", "findings": 2, "applied": 0, "sidecar_path": "s1.md"}]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_only_pre_existing_or_kept_failures_keep_the_findings_text():
    wave = [{"slice_id": "s1", "verdict": "BLOCKED", "findings": 2, "applied": 0, "sidecar_path": "s1.md"}]
    halted = _run(wave, {"failing": ["a.py::old", "a.py::kept"], "pre_existing": ["a.py::old"], "kept": ["a.py::kept"]})

    assert "blocker" not in halted
    assert halted["unapplied_findings"] == 2
    assert "re-runs the review wave" in json.dumps(halted["next_action"])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_failing_nodes_and_no_blocked_slice_does_not_halt():
    assert _run([], {"failing": [], "pre_existing": [], "kept": []}) is None
