"""bash_guard_commit_path_spawns: spawn attribution, leg tracking and the report shape through the real hook entry."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks import bash_guard_commit_path_spawns as bench
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("s\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_hooks_count_real_popen_and_restore_the_originals(repo) -> None:
    original = subprocess.Popen.__init__
    rec = bench._Recorder()
    with bench._hooks(rec):
        subprocess.run(["git", "-C", str(repo), "status"], capture_output=True, check=True)
    assert len(rec.git) == 1
    assert subprocess.Popen.__init__ is original


def test_measure_through_the_hook_entry_reports_the_contract_keys(repo) -> None:
    bench._write_fixture(repo)
    report = bench.measure(repo)
    assert {
        "git_spawns", "git_argv", "other_spawns", "legs", "parent_ms",
        "children_ms", "total_process_ms", "warm_waits", "verdict", "rewritten",
    } <= set(report)
    assert report["command"] == bench.R1_COMMAND
    assert report["git_spawns"] == len(report["git_argv"])
    assert report["git_spawns"] == sum(row["git_spawns"] for row in report["legs"])
    assert report["total_process_ms"] == pytest.approx(report["parent_ms"] + report["children_ms"], abs=0.01)
    assert isinstance(report["warm_waits"], list)
    assert report["legs"], "the chain bracketed no leg"
