"""The roster judge rides inside the one verify agent on the M and L arms, never gated off."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch, arm, **extra) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    monkeypatch.setattr(ask_compose, "head_sha", lambda _root: "h" * 40)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **extra,
    )


@pytest.mark.parametrize("arm", ["m", "l"])
def test_the_judge_brief_is_inside_the_one_verify_agent(monkeypatch, arm):
    script = _script(monkeypatch, arm, **_JUDGE_KW)
    assert script.count("label: 'verify'") == 1
    assert "agentType: 'coordinator:criterion-judge'" not in script
    assert "_falsifierResult = (_verify && _verify.judge) ? " in script
    assert "`judge` (the criterion verdict fields the judge brief above names" in script
    assert "unjudged: verify leg returned no verdict" in script
    assert "judge" in script[script.index("properties") :]


@pytest.mark.parametrize("arm", ["m", "l"])
def test_a_judgeless_roster_leaves_the_judge_out_on_the_same_arms(monkeypatch, arm):
    script = _script(monkeypatch, arm, **REVIEW_KW)
    assert "_verify.judge" not in script
    assert "`judge`" not in script
    assert script.count("label: 'verify'") == 1
