"""An evidence append on the located plan's sidecar after halt is the run's own artifact, not foreign dirt.

The halted record and the sizing edge name the planner's flat string; the plan lives at its `<YYYY-MM>` shard.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import run_artifact_ownership as own
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261112T225815-f4df26"
SIZING = "state/sizings/2026-11/2026-11-12-my-plan.yaml"
REPORTED_PLAN = "docs/plans/2026-11-12-my-plan.md"
LOCATED_PLAN = "docs/plans/2026-11/2026-11-12-my-plan.md"
LOCATED_EVIDENCE = "docs/plans/2026-11/2026-11-12-my-plan.evidence.yaml"
OTHER_EVIDENCE = "docs/plans/2026-11/2026-11-12-other-plan.evidence.yaml"


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True, **no_console_creationflags())


def _build(tmp_path: Path) -> Path:
    """A run halted naming `REPORTED_PLAN` (sizing edge and halted.json alike) whose plan is at `LOCATED_PLAN`."""
    root = tmp_path / "repo"
    (root / "docs/plans/2026-11").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / LOCATED_PLAN).write_text("# plan\n", encoding="utf-8")
    for rel in (LOCATED_EVIDENCE, OTHER_EVIDENCE):
        (root / rel).write_text("criterion_evidence: {}\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text(json.dumps({"write_baseline": {}, "rows": []}), encoding="utf-8")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (root / SIZING).parent.mkdir(parents=True)
    (root / SIZING).write_text(f'plan: "{REPORTED_PLAN}"\n', encoding="utf-8")
    (rdir / "halted.json").write_text(json.dumps({"plan": REPORTED_PLAN}), encoding="utf-8")
    return root


def test_evidence_beside_the_located_plan_is_in_the_runs_plan_evidence(tmp_path):
    root = _build(tmp_path)
    owned = own.own_artifact_paths(root, RUN)
    assert LOCATED_EVIDENCE in owned.plan_evidence, (
        f"evidence path absent from plan_evidence: {sorted(owned.plan_evidence)} names the reported plan's "
        f"sidecar, not the located plan's {LOCATED_EVIDENCE}"
    )
    assert owned.covers(LOCATED_EVIDENCE)


def test_another_plans_evidence_stays_foreign(tmp_path):
    root = _build(tmp_path)
    assert not own.own_artifact_paths(root, RUN).covers(OTHER_EVIDENCE)


def test_post_halt_append_on_the_located_evidence_yields_no_foreign_hunk(tmp_path):
    root = _build(tmp_path)
    for rel in (LOCATED_EVIDENCE, OTHER_EVIDENCE):
        (root / rel).write_text("criterion_evidence: {C4: ok}\n", encoding="utf-8")

    found = hp.unproduced_hunks(root, RUN, [LOCATED_EVIDENCE, OTHER_EVIDENCE], own_plan=REPORTED_PLAN, advisories=[])

    assert [f.path for f in found] == [OTHER_EVIDENCE]
