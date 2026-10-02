"""Exact-equality spawn count for `roadmap.blitz_stage`, through the real commit_v2.

Two scoped commits (stubs, then the frozen report) spawn no git process: commit_v2 stages in
process. The count does not grow with the number of stubs. The figure is read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import roadmap_blitz_stage
from coordinator_core.roadmap.tests.test_blitz_stage import CLUSTERS, RECON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _budget() -> dict:
    return load_manifest()["overrides"]["roadmap.blitz_stage"]["spawn_count_budget"]


def test_staging_a_roadmap_commits_stubs_and_report_with_the_budgeted_spawns(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "init")
    run = tmp_path / "state" / "roadmap" / "rm-test"
    run.mkdir(parents=True)
    (run / "clusters.md").write_text(CLUSTERS, encoding="utf-8")
    (run / "reconciliation.md").write_text(RECON, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "00000000-0000-4000-8000-000000000000")

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = roadmap_blitz_stage._handler({"roadmap": str(run)}, repo_root=tmp_path / ".git")

    assert out["commit_error"] is None and len(out["commits"]) == 2, out
    assert out["gate_report_state"] == "frozen-now"
    # The quarantined HOME has no machine-local registry, so doc-new's registry load falls to its
    # subprocess rung; an installed machine answers it in process.
    counted = [s for s in spawns if "_machine_local.py" not in " ".join(s.argv)]
    assert len(counted) == sum(_budget().values()), [s.argv for s in counted]
    tracked = _git(tmp_path, "ls-files").splitlines()
    assert out["gate_report_path"] in tracked and all(s["path"] in tracked for s in out["stubs"])
