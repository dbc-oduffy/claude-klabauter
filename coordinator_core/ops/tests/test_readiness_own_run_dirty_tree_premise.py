"""A resumed run's own withheld writes, still dirty in the tree, never contradict its premises."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import _anchor_premise as ap
from coordinator_core.ops import _own_run_premise
from coordinator_core.ops.tests.test_readiness_own_run_withheld_premise import (
    RUN, SIZING, _repo, _reply, _row,
)

ANCHOR = {"id": "AP1", "claim": "c", "check": {"command": "grep -c refuse mod.py", "expect": "1"}}
PLAN = f"---\nsizing_object: {SIZING}\nprime_exit_criterion:\n  statement: s\n---\n# p\n"


def _resumed(root: Path, withheld: dict, base: str = "") -> None:
    """A predecessor marker as ``mark_resumed`` leaves it: status flipped, withheld kept under
    ``run_artifacts``, the withheld index entry gone."""
    record = root / "state" / "warp" / RUN
    record.mkdir(parents=True)
    marker = {"status": "resumed", "sizing_rel": SIZING, "run_artifacts": {"withheld": withheld}}
    if base:
        marker["run_base_sha"] = base
    (record / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _dirty_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("refuse here\n")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-q", "-m", "base")
    sha = _git(root, "rev-parse", "HEAD")
    (root / "mod.py").write_text("converted\n")
    return sha


def test_resumed_run_withheld_paths_are_its_own_writes(tmp_path):
    _resumed(tmp_path, {"src/x.py": "peer-hold"})
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/x.py": RUN}


def test_resumed_run_withheld_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _resumed(tmp_path, {"src/x.py": "peer-hold"})
    assert _reply(common, [_row("src/x.py")])["verdict"] == "ready"
    assert _reply(common, [_row("src/y.py")])["verdict"] == "judgment"


@pytest.mark.spawns_process
def test_dirty_tree_anchor_of_a_resumed_run_holds_at_its_base(tmp_path):
    base = _dirty_repo(tmp_path)
    _resumed(tmp_path, {"mod.py": "withheld"}, base)
    (v,) = ap.anchor_verdicts([ANCHOR], [], [], PLAN, tmp_path)
    assert v["verdict"] == "RESOLVES" and v["observed"].startswith(ap.REANCHORED)


@pytest.mark.spawns_process
def test_dirty_tree_anchor_on_a_path_the_run_did_not_write_still_contradicts(tmp_path):
    base = _dirty_repo(tmp_path)
    _resumed(tmp_path, {"other.py": "withheld"}, base)
    (v,) = ap.anchor_verdicts([ANCHOR], [], [], PLAN, tmp_path)
    assert v["verdict"] == "CONTRADICTED"
