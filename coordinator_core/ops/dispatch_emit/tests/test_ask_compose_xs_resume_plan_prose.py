"""A gate reply's prose `resume_plan` is halted at the xs/S adoption sites, never adopted as the plan path."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, unshare_regex_literals

_NODE = shutil.which("node")
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _shape(values: list) -> list[bool]:
    js = f"const f = {ask_compose._RESUME_PLAN_SHAPE_JS}; console.log(JSON.stringify({json.dumps(values)}.map(f)));"
    out = subprocess.run(
        [_NODE, "-e", js], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_the_shape_predicate_accepts_a_repo_relative_plan_and_refuses_prose() -> None:
    good = "docs/plans/2026-11/x.md"
    prose = "Resume the existing plan and add the missing row"
    drive = "C:" + "/x.md"
    assert _shape([good, prose, "../x.md", "/abs/x.md", drive, "docs/x.yaml", None, ""]) == [
        True, False, False, False, False, False, False, False,
    ]


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("arm", ["xs", "s"])
def test_the_composed_script_halts_a_prose_resume_plan_at_the_adoption_site(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        in_run_commit=True, **REVIEW_KW,
    )
    script = unshare_regex_literals(script)
    halt = "gate resume_plan is not a repo-relative plan path"
    guarded = f"if ({ask_compose._RESUME_PLAN_SHAPE_JS}(_gate.resume_plan))"
    assert halt in script
    assert script.count(guarded) >= 1
    assert "{ _planRel = _gate.resume_plan; }" not in script.replace(f"{guarded} {{ _planRel = _gate.resume_plan; }}", "")
