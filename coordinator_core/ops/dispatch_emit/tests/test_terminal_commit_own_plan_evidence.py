"""The run's own plan evidence file has no write_baseline yet is its own artifact; another plan's still refuses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import run_artifact_ownership as own
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261031T225815-f4df26"
SIZING = "state/sizings/2026-10-31-p-1-a.yaml"
PLAN = "docs/plans/2026-10-31-my-plan.md"
EVIDENCE = "docs/plans/2026-10-31-my-plan.evidence.yaml"
OTHER_EVIDENCE = "docs/plans/2026-10-31-other-plan.evidence.yaml"


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True, **no_console_creationflags())


def _build(tmp_path: Path, *, edge: str | None = PLAN, halted_plan: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "docs/plans").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for rel in (EVIDENCE, OTHER_EVIDENCE):
        (root / rel).write_text("criterion_evidence: {}\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(json.dumps({"write_baseline": {}, "rows": []}), encoding="utf-8")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (root / "state/sizings").mkdir(parents=True)
    body = f'plan: "{edge}"  # the plan\n' if edge else "title: x\n"
    (root / SIZING).write_text(body, encoding="utf-8")
    if halted_plan:
        (rdir / "halted.json").write_text(json.dumps({"plan": halted_plan}), encoding="utf-8")
    return root


def test_sizing_plan_edge_makes_evidence_own(tmp_path):
    root = _build(tmp_path)
    assert own.own_artifact_paths(root, RUN).covers(EVIDENCE)
    assert not own.own_artifact_paths(root, RUN).covers(OTHER_EVIDENCE)


def test_halted_plan_makes_evidence_own(tmp_path):
    root = _build(tmp_path, edge=None, halted_plan=PLAN)
    assert own.own_artifact_paths(root, RUN).covers(EVIDENCE)


def test_unproduced_hunks_accepts_own_evidence_and_refuses_other(tmp_path):
    root = _build(tmp_path)
    for rel in (EVIDENCE, OTHER_EVIDENCE):
        (root / rel).write_text("criterion_evidence: {C6: ok}\n", encoding="utf-8")
    advisories: list = []

    found = hp.unproduced_hunks(root, RUN, [EVIDENCE, OTHER_EVIDENCE], own_plan=PLAN, advisories=advisories)

    assert [f.path for f in found] == [OTHER_EVIDENCE]
    assert found[0].detail.startswith("provenance unreadable")


def test_no_plan_edge_leaves_evidence_refused(tmp_path):
    root = _build(tmp_path, edge=None)
    (root / EVIDENCE).write_text("criterion_evidence: {C6: ok}\n", encoding="utf-8")

    found = hp.unproduced_hunks(root, RUN, [EVIDENCE], own_plan=None)

    assert [f.path for f in found] == [EVIDENCE]
