"""Exact-equality git spawn count for `plan.seam_check` and `plan.seam_record`: one batched
`cat-file --batch-check` per call however many plans and paths, plus one name-only diff at
wave-boundary. The figures are read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_seam_check as op

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ROW = "- id: R{j}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: [m{i}/f{j}.py]\n  consumes: [lib{i}/c{j}.py]\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _fixture(root: Path) -> list:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    plans = []
    for i in range(4):
        rel = f"docs/plans/2026-10-08-p{i}.md"
        (root / "docs/plans").mkdir(parents=True, exist_ok=True)
        rows = "".join(ROW.format(i=i, j=j) for j in range(3))
        (root / rel).write_text(
            "---\ntitle: t\nstatus: approved\ncapabilities: []\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
            + rows + "```\n", encoding="utf-8")
        (root / f"lib{i}").mkdir()
        for j in range(3):
            (root / f"lib{i}/c{j}.py").write_text("x", encoding="utf-8")
        plans.append(rel)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / "touched.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "wave")
    return plans


def _budget(name: str, key: str) -> int:
    return load_manifest()["overrides"][name]["spawn_count_budget"][key]


def _params(plans, root, phase):
    out = {"plans": plans, "phase": phase, "named_set": True}
    if phase == "wave-boundary":
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD~1", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.split()
        out.update(landed_range=f"{sha[0]}..{sha[1]}", landed_rows=[], wave=1)
    return out


def _assert(spawns, expected):
    assert len(spawns) == expected, [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert sum("cat-file" in s.argv for s in spawns) == 1
    assert sum("diff" in s.argv for s in spawns) == expected - 1


@pytest.mark.parametrize("phase,key", [("prep", "prep_fire"), ("wave-boundary", "wave_boundary")])
def test_seam_check_spawns_exactly_the_budgeted_batches(tmp_path, monkeypatch, phase, key):
    plans = _fixture(tmp_path)
    params = _params(plans, tmp_path, phase)
    with _count_spawns_attributed(monkeypatch) as spawns:
        reply = op._check_handler(params, tmp_path)
    assert reply["verdict"] == "CLEAN", reply["findings"]
    _assert(spawns, _budget("plan.seam_check", key))


@pytest.mark.parametrize("phase,key", [("prep", "prep_fire"), ("wave-boundary", "wave_boundary")])
def test_seam_record_spawns_exactly_the_budgeted_batches(tmp_path, monkeypatch, phase, key):
    plans = _fixture(tmp_path)
    params = _params(plans, tmp_path, phase)
    with _count_spawns_attributed(monkeypatch) as spawns:
        reply = op._record_handler(params, tmp_path)
    assert len(reply["sidecars"]) == len(plans)
    _assert(spawns, _budget("plan.seam_record", key))
