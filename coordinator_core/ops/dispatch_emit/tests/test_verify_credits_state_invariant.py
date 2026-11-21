"""A run landing state/* run artifacts whose verify passed lands without a hand retest."""
from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


RUN_ID = "ask-20261027T000000-abc123"
INVARIANT = "coordinator_core/session/tests/test_machinery_paths.py"
NODE = f"{INVARIANT}::test_every_tracked_state_first_segment_is_on_the_allowlist"
SIZING = "state/sizings/2026-10-27-fixture.yaml"
PRODUCT = "state/lessons/2026-10-27-fixture.md"
PARAMS = {"targeted_tests": {"status": "fail", "baseline": None}}
PARAMS_PASS = {"targeted_tests": {"status": "pass"}}


@pytest.mark.skip(reason="default-leg wiring (R1) withheld; carried by ask-20261027T172717-78e815")
def test_the_default_ask_verify_leg_launches_and_polls_the_retest():
    script = ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Prove the landing credits its invariants",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    assert script.count("retest_targeted --from-task-output") == 1
    assert "retest_targeted --poll --from-task-output" in script
