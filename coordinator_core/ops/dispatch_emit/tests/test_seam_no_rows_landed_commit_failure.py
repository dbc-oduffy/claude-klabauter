"""A wave whose rows landed nothing reports a commit failure, never a seam failure."""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops.dispatch_emit.tests import test_seam_wave_boundary as sw

_NODE = sw._NODE

_OLD = "return label.startsWith('work:') ? 'DONE: ok' : null;"
assert _OLD in sw._HARNESS
_HARNESS = sw._HARNESS.replace(
    _OLD,
    "return label.startsWith('work:') ? (label === 'work:P1-C1' || label === 'work:P3-C1' ? 'BLOCKED: no' : 'DONE: ok') : null;",
)


def test_the_no_commit_landed_seam_failure_is_gone():
    script = sw._compose(sw._two_plan_waves())
    assert "_seamFail(n, 'no commit landed')" not in script
    assert "_seamFail(n, " in script


@_NODE
def test_a_wave_landing_no_rows_holds_its_dependent_as_commit_failed_with_no_seam_failure(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    monkeypatch.setattr(sw, "_HARNESS", _HARNESS)
    out = sw._run_script(tmp_path, sw._compose(sw._two_plan_waves()), sw.CLEAN_REPLY)
    assert "seam:wave-1" not in out["calls"] and "commit:wave-1" not in out["calls"]
    assert "work:P3-C2" in out["calls"]
    assert out["failures"] == [] and out["drift"] == [] and out["haltedPlans"] == []
    assert out["commitFailures"] == ["wave 1: no row landed to commit"]
    assert "work:P2-C1" not in out["calls"]
    assert out["commitRefused"]["wave 1 commit"] == "wave 1: no row landed to commit"
