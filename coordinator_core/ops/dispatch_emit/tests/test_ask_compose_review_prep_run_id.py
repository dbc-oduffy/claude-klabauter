"""The composed ask review-wave prep command names the run by run_id, so its length is constant in the footprint."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.review_prep import _handler as review_prep, declared_paths_for_run
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PREP_COMMAND_MAX_CHARS = 1024
_RUN = "run-1"
_PLAN_REL = "docs/plans/p.md"
_SESSION_ID = "0123456789abcdef0123456789abcdef0123"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_FLAGS = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    plan = tmp_path / _PLAN_REL
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".coordinator-local/\nstate/\nscratch/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _compose(root: Path) -> str:
    return compose_ask_script(
        repo_root=root.as_posix(),
        prompt="add a widget",
        sizing_rel=None,
        run_id=_RUN,
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _prep_command(script: str) -> str:
    hit = re.search(r"coordinator-invoke\" review\.prep \\'(\{.*?\})\\'", script)
    assert hit, "no review.prep command in the composed script"
    return hit.group(0)


def _params(command: str) -> dict:
    text = re.search(r"\\'(\{.*\})\\'", command).group(1).replace("\\'", "'")
    return json.loads(text.replace("<plan_path>", _PLAN_REL))


def _stage(root: Path, declared: list[str], footprint: list[str] | None = None) -> None:
    base = record_dir(root, _RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")
    if footprint is not None:
        (base / LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": footprint}), encoding="utf-8")


def test_prep_command_is_constant_in_the_footprint(repo):
    command = _prep_command(_compose(repo))
    params = _params(command)
    assert params["run_id"] == _RUN and "paths" not in params
    assert len(command.replace("<plan_path>", _PLAN_REL)) + len(_SESSION_ID) <= PREP_COMMAND_MAX_CHARS

    many = [f"pkg{i % 9}/mod{i}.py" for i in range(200)]
    _stage(repo, many)
    assert declared_paths_for_run(repo, _RUN) == many
    large = _prep_command(_compose(repo))

    _stage(repo, ["pkg/one.py"])
    assert declared_paths_for_run(repo, _RUN) == ["pkg/one.py"]
    assert _prep_command(_compose(repo)) == large == command


def test_composed_params_return_the_same_slices_as_explicit_paths(repo):
    rels = [f"pkg{i % 3}/mod{i}.py" for i in range(3)]
    for rel in [*rels, "leg/extra.py"]:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v = 1\n" * 20, encoding="utf-8")
    _stage(repo, rels)
    composed = {**_params(_prep_command(_compose(repo))), "session_id": "sess-own"}
    assert composed["run_id"] == _RUN
    explicit = {k: v for k, v in composed.items() if k != "run_id"}
    explicit.update(paths=rels, slice_prefix=run_prep_slice_prefix(_RUN, composed["base"]))
    by_id = review_prep(composed, repo_root=repo)
    by_paths = review_prep(explicit, repo_root=repo)
    assert by_id["error"] is None and by_paths["error"] is None, (by_id["error"], by_paths["error"])
    assert by_id["slices"]

    def shape(reply):
        return [(s["id"].split("-", 2)[-1], s["files"]) for s in reply["slices"]], reply["product_files"]

    assert shape(by_id) == shape(by_paths)

    _stage(repo, rels, footprint=["leg/extra.py"])
    legged = review_prep({**composed, "leg": True, "slice_prefix": "prep-leg"}, repo_root=repo)
    assert legged["error"] is None
    assert "leg/extra.py" in [f for s in legged["slices"] for f in s["files"]]
    assert "leg/extra.py" not in [f for s in by_id["slices"] for f in s["files"]]


def test_leg_flag_rides_the_prompt_tail_only_on_a_leg_fire(repo):
    script = _compose(repo)
    decl = re.search(r"const _prepLeg = (.+);\n", script).group(1)
    assert "remaining_leg?.writes" in decl and "' leg: true'" in decl
    assert script.index("const _prepLeg") < script.index("phase('Review wave')")
    assert "+ _prepLeg" in script
