"""compose_ask_script: a pull whose review is OK is patched in place; only a PIVOT or ABORT review re-plans."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _PULL_REVIEW_UNSAVEABLE_JS, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _pulled_block() -> str:
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1",
        session_id=None, wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    return script[script.index("_pulledEntry = ") : script.index("plan-blitz reported no ready plan")]


def test_revise_and_reblitz_sit_only_under_the_unsaveable_guard():
    block = _pulled_block()
    assert f"const _unsaveable = {_PULL_REVIEW_UNSAVEABLE_JS};" in block
    guard = block.index("if (_unsaveable) { _revised = await")
    revise = block.index("label: 'plan-revise'")
    patch = block.index("label: 'plan-patch'")
    assert block.count("label: 'plan-revise'") == 1 and block.count("await planBlitz(") == 1
    assert guard < revise < block.index("else { _revised = await") < patch
    reblitz_guard = block.index("if (_unsaveable) {", patch)
    assert reblitz_guard < block.index("await planBlitz(")


def test_ok_branch_adopts_on_ready_with_pull_class_none_or_mechanical():
    block = _pulled_block()
    assert (
        "_revised?.verdict === 'ready' && (_revised?.pull_class === 'mechanical' || _revised?.pull_class === 'none')"
        in block
    )
    adopt = block.index("_planRel = _revised.plan_rel")
    assert adopt < block.index("await planBlitz(")


def test_patch_leg_does_not_use_the_opus_plan_author():
    block = _pulled_block()
    patch = block[block.index("label: 'plan-patch'") :]
    patch = patch[: patch.index("}")]
    assert "agentType: 'general-purpose'" in patch or 'agentType: "general-purpose"' in patch
    assert "plan-author" not in patch


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "reviews,expect",
    [
        ([{"reviewer": "staff-eng", "verdict": "OK"}], False),
        # BLOCKED patches rather than re-plans (plan R2/N2 narrows the PM's "abort or blocked").
        ([{"reviewer": "staff-eng", "verdict": "BLOCKED"}], False),
        ([{"reviewer": "premise-check", "verdict": "BLOCKED"}], False),
        ([{"reviewer": "premise-check", "verdict": "ABORT"}], False),
        ([{"reviewer": "staff-eng", "verdict": "pivot"}], True),
        ([{"reviewer": "staff-eng", "verdict": "OK"}, {"reviewer": "x", "verdict": "ABORT"}], True),
        ([{"reviewer": "x", "verdict": "REJECTED"}], False),
        ([{"reviewer": "x", "verdict": "REJECTED", "premiseFailure": "census gone"}], True),
        (None, False),
        ([{"reviewer": "x", "verdict": "REJECTED", "premiseFailure": ["census gone"]}], True),
        ([{"reviewer": "x", "verdict": "REJECTED", "premiseFailure": []}], False),
    ],
)
def test_unsaveable_predicate(reviews, expect):
    entry = {} if reviews is None else {"reviews": reviews}
    js = f"const _pulledEntry = {json.dumps(entry)}; console.log(JSON.stringify({_PULL_REVIEW_UNSAVEABLE_JS}));"
    out = subprocess.run(
        ["node", "-"], input=js, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    assert json.loads(out) is expect
