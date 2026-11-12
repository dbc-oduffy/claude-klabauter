"""The blitz leg's `_planRel` is the plan's located path, not the planner's returned string.

Drives the composed script's own `_planRel` assignment in node over a repo whose plan lives at its `<YYYY-MM>`
shard while the planner returned the flat dated string.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.sizing_fire import PLAN_PATH_JS, s_plan_located, s_plan_path
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.usefixtures("m_plus_known_arm"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

SIZING = "state/sizings/2026-11/2026-11-12-my-plan.yaml"
PLANNER_STRING = "docs/plans/2026-11-12-my-plan.md"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(repo_root: str) -> str:
    return compose_ask_script(
        repo_root=repo_root, prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", **REVIEW_KW,
    )


def _assignment(script: str) -> str:
    """The script's first blitz-leg `_planRel` assignment line."""
    return next(ln for ln in script.split("\n") if ln.startswith("    _planRel = (_ready &&"))


def _drive(root, ready_js: str) -> str | None:
    program = (
        f"const REPO_ROOT = {json.dumps(str(root))}; const _sizingRel = {json.dumps(SIZING)};\n"
        f"{PLAN_PATH_JS}\nlet _planRel = null; const _ready = {ready_js};\n"
        f"{_assignment(_script(str(root)))}\nconsole.log(JSON.stringify({{ planRel: _planRel }}));\n"
    )
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)["planRel"]


@pytest.fixture
def repo(tmp_path):
    located = s_plan_path(SIZING)
    (tmp_path / located).parent.mkdir(parents=True)
    (tmp_path / located).write_text("# plan\n", encoding="utf-8")
    assert s_plan_located(tmp_path, SIZING) == located != PLANNER_STRING
    return tmp_path


@pytest.mark.parametrize("shape", ["object", "bare-string"])
def test_planrel_is_the_located_path_when_the_planner_returns_a_non_located_string(repo, shape):
    ready = json.dumps({"planPath": PLANNER_STRING}) if shape == "object" else json.dumps(PLANNER_STRING)
    got = _drive(repo, ready)
    assert got == s_plan_located(repo, SIZING), (
        f"_planRel != located path: the blitz leg kept the planner's string {got!r}, "
        f"the plan lives at {s_plan_located(repo, SIZING)!r}"
    )


def test_planrel_stays_the_planners_path_when_that_path_is_the_located_one(repo):
    located = s_plan_located(repo, SIZING)
    assert _drive(repo, json.dumps({"planPath": located})) == located
