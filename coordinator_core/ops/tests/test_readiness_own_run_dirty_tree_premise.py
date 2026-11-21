"""A resumed run's own withheld writes, still dirty in the tree, never contradict its premises."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_resumed_run_withheld_paths_are_its_own_writes(tmp_path):
    _resumed(tmp_path, {"src/x.py": "peer-hold"})
    assert _own_run_premise.own_run_paths(tmp_path, SIZING) == {"src/x.py": RUN}


def test_resumed_run_withheld_path_row_is_not_a_pull(tmp_path):
    common = _repo(tmp_path)
    _resumed(tmp_path, {"src/x.py": "peer-hold"})
    assert _reply(common, [_row("src/x.py")])["verdict"] == "ready"
    assert _reply(common, [_row("src/y.py")])["verdict"] == "judgment"
