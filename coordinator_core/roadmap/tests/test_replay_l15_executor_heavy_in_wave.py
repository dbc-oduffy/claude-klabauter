"""Replay of the r40/r47/r51 EXECUTOR_HEAVY pulls: the gate's repair is mechanical and a fixpoint."""

import pytest

from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.roadmap.tests import _executor_heavy_shapes as shapes

HEAVY = "EXECUTOR_HEAVY:executor-heavy-command-row"
SHAPES = {
    "PURE_RETYPE": shapes.PURE_RETYPE,
    "WRITES_SPLIT": shapes.WRITES_SPLIT,
    "BUILD_CLASS": shapes.BUILD_CLASS,
    "R40": shapes.R40,
    "R47": shapes.R47,
    "R51": shapes.R51,
    "SEMICOLON_CMD": shapes.SEMICOLON_CMD,
}
ROLES = {
    "without_build_measure_role": {"executor": "", "test-runner": ""},
    "with_build_measure_role": {"executor": "", "test-runner": "", "builder": "admits: [build, measure]\n"},
}


def _verdict(gated):
    return op.readiness_verdict(gated, None, [])


@pytest.mark.parametrize("roles", ROLES.values(), ids=ROLES.keys())
@pytest.mark.parametrize("spine", SHAPES.values(), ids=SHAPES.keys())
def test_executor_heavy_is_repaired_in_wave_and_regates_clean(tmp_path, monkeypatch, spine, roles):
    shapes.agents(tmp_path, monkeypatch, roles)
    gated = shapes.gate(tmp_path, spine)
    assert gated["classes"]["EXECUTOR_HEAVY"]["status"] != "PASS"

    out = _verdict(gated)
    assert out["pull_class"] == "mechanical", out
    assert HEAVY not in out["blocking"]
    assert any(r["kind"] == "executor-heavy-command-row" for r in out["mechanical_repairs"])

    repair = gated["classes"]["EXECUTOR_HEAVY"]["repair"]
    repaired = tmp_path / "repaired"
    repaired.mkdir()
    regated = shapes.gate(repaired, shapes.apply_repair(spine, repair))
    assert regated["classes"]["EXECUTOR_HEAVY"]["status"] == "PASS", regated["classes"]["EXECUTOR_HEAVY"]
    assert regated["classes"]["TEST_RUNNER_OUT_OF_ROLE"]["status"] == "PASS"
    assert _verdict(regated)["pull_class"] != "substantive"
