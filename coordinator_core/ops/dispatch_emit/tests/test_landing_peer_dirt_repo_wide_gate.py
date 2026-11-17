"""A fresh red the run's retest could not attribute is judged from the retest record's ``owed_cleared``
(the child's HEAD plus landing paths measurement); the landing never runs pytest. Green there lands (a peer's
dirt); measured and not green is the run's own bytes and refuses; unmeasured stays unknown, never cleared."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li

RUN_ID = "run-peer-dirt"
GATE = "tests/test_gate.py"
GATE_NODE = f"{GATE}::test_the_live_gate_is_green"
OWN = "src/own.py"
PEER = "src/peer.py"


GATE = "tests/test_gate.py"
GATE_NODE = f"{GATE}::test_the_live_gate_is_green"
OWN = "src/own.py"
RUN_ID = "run-peer-dirt"


def _fresh_red(root: Path, **measured) -> dict:
    """A fresh engine-written fail record whose gate red has no baseline evidence; ``measured`` adds retest-child keys."""
    li.fresh_verdict_path(root, RUN_ID).parent.mkdir(parents=True, exist_ok=True)
    li.fresh_verdict_path(root, RUN_ID).write_text(json.dumps({
        "run_id": RUN_ID, "head": "h1", "covered": {},
        "status": "fail", "tests": [GATE], "failed_tests": [GATE_NODE], "tests_run": 3, "tests_failed": 1,
        "baseline": {"base_sha": "c" * 40, "caused": [], "foreign": [GATE_NODE], "evidence": [], **measured},
    }), encoding="utf-8")
    return {}


@pytest.fixture(autouse=True)
def _fixed_head(monkeypatch):
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: "h1")


def test_a_gate_red_green_at_head_plus_landing_paths_lands(tmp_path):
    params = _fresh_red(tmp_path, owed_cleared=[GATE_NODE])

    findings, check = li.landing_test_gate(tmp_path, params, RUN_ID)

    assert not [f for f in findings if f.reason == li.TARGETED_TESTS_FAILED]
    assert check == "pre-existing-only"


def test_a_gate_red_by_the_runs_own_bytes_still_refuses(tmp_path):
    params = _fresh_red(tmp_path, owed_cleared=[])

    findings, check = li.landing_test_gate(tmp_path, params, RUN_ID)

    assert check == "failed"
    assert [f.reason for f in findings] == [li.TARGETED_TESTS_FAILED]
    assert GATE_NODE in findings[0].detail


def test_a_red_the_retest_did_not_measure_is_unknown_not_cleared(tmp_path):
    params = _fresh_red(tmp_path)

    findings, check = li.landing_test_gate(tmp_path, params, RUN_ID)

    assert check == "unknown-only" and list(findings) == [] and findings.unknown_ids == [GATE_NODE]
