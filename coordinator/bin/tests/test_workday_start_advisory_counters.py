"""test_workday_start_advisory_counters — pytest tests for
workday-start-advisory-counters.py (WDS-3: coordinator-claude /workday-start bash-block
extirpation — improvement-queue depth, push-failure log stats, local-only-ahead
branch check, all ported to one naked-Python CLI).

Coverage:
  improvement-queue
    - central resolved, entries present, oldest = earliest dated filename.
    - central unresolved (coordinator_state_root_central() returns "") -> error
      field populated, count 0 — "surface to PM, do not report the queue as
      empty" contract.
    - local dir absent -> present False, count 0.
    - `recurring` per-entry field (>=3) surfaced from both central and local
      queues; bool values excluded (bool is an int subtype in Python).
    - cross-repo-commitments: open-count + oldest-days computed from `observed`.
  push-failures
    - delegates to coordinator_core.ops.workday_surface_auto_push_failure_stats
      (this test monkeypatches that function, not its internals — this file's
      own job is only the CLI's argv/stdout/error-degrade plumbing around it).
  local-ahead
    - branch not work/*|feature/* -> eligible False, no git calls made.
    - branch ahead of origin -> ahead_count computed from `origin/<branch>..HEAD`.
    - branch absent on origin -> no_origin True, ahead_count = all local commits.

Run: python3 -m pytest coordinator/bin/tests/test_workday_start_advisory_counters.py -q
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

# Declared, not excused: 3 of this file's tests (test_local_ahead_*) spawn a real git
# process because the property under test is git's own ahead-count/no-origin
# resolution against a real branch/remote, which no mock stands in for. Each builds
# its own small repo (3-9 git calls) exercising a genuinely different branch/remote
# scenario, so there is no shared state to hoist to module scope without conflating
# those scenarios. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    """Load workday-start-advisory-counters.py by file path (hyphenated name)."""
    spec = importlib.util.spec_from_file_location(
        "workday_start_advisory_counters",
        _BIN_DIR / "workday-start-advisory-counters.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=str(path), check=True)


# ---------------------------------------------------------------------------
# improvement-queue
# ---------------------------------------------------------------------------

def test_improvement_queue_central_resolved_and_oldest(tmp_path, monkeypatch):
    central_state = tmp_path / "central-state"
    iq_dir = central_state / "improvement-queue"
    iq_dir.mkdir(parents=True)
    (iq_dir / "2026-06-01-first.yaml").write_text("title: First\nbody: x\n", encoding="utf-8")
    (iq_dir / "2026-07-01-second.yaml").write_text("title: Second\nbody: y\n", encoding="utf-8")

    monkeypatch.setattr(
        "coordinator_core.state_root.coordinator_state_root_central",
        lambda: str(central_state),
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = _mod.cmd_improvement_queue(repo_root)

    assert result["central"]["resolved"] is True
    assert result["central"]["count"] == 2
    assert result["central"]["oldest"] == "2026-06-01-first.yaml"
    assert result["central"]["error"] is None
    assert result["local"]["present"] is False
    assert result["local"]["count"] == 0


def test_improvement_queue_central_unresolved_surfaces_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.state_root.coordinator_state_root_central",
        lambda: "",
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    result = _mod.cmd_improvement_queue(repo_root)

    assert result["central"]["resolved"] is False
    assert result["central"]["count"] == 0
    assert result["central"]["error"] is not None
    assert "do not report the queue as empty" in result["central"]["error"]


def test_improvement_queue_recurring_field_surfaced_and_bool_excluded(tmp_path, monkeypatch):
    central_state = tmp_path / "central-state"
    iq_dir = central_state / "improvement-queue"
    iq_dir.mkdir(parents=True)
    (iq_dir / "2026-06-01-recurring.yaml").write_text(
        "title: Recurring thing\nrecurring: 4\n", encoding="utf-8"
    )
    (iq_dir / "2026-06-02-bool-recurring.yaml").write_text(
        "title: Bool flagged, not a count\nrecurring: true\n", encoding="utf-8"
    )
    (iq_dir / "2026-06-03-below-threshold.yaml").write_text(
        "title: Only twice\nrecurring: 2\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "coordinator_core.state_root.coordinator_state_root_central",
        lambda: str(central_state),
    )

    repo_root = tmp_path / "repo"
    local_iq_dir = repo_root / "state" / "improvement-queue"
    local_iq_dir.mkdir(parents=True)
    (local_iq_dir / "2026-06-04-local-recurring.yaml").write_text(
        "title: Local recurring\nrecurring: 5\n", encoding="utf-8"
    )

    result = _mod.cmd_improvement_queue(repo_root)

    recurring_titles = {(entry["scope"], entry["title"]) for entry in result["recurring"]}
    assert ("central", "Recurring thing") in recurring_titles
    assert ("local", "Local recurring") in recurring_titles
    assert not any(title == "Bool flagged, not a count" for _, title in recurring_titles)
    assert not any(title == "Only twice" for _, title in recurring_titles)
    assert result["local"]["present"] is True
    assert result["local"]["count"] == 1


def test_improvement_queue_commitments_open_count_and_oldest_days(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.state_root.coordinator_state_root_central",
        lambda: "",
    )
    repo_root = tmp_path / "repo"
    commitments_dir = repo_root / "state" / "cross-repo-commitments"
    commitments_dir.mkdir(parents=True)
    (commitments_dir / "a.yaml").write_text(
        "status: open\nobserved: \"2026-07-01\"\n", encoding="utf-8"
    )
    (commitments_dir / "b.yaml").write_text(
        "status: open\nobserved: \"2026-07-10\"\n", encoding="utf-8"
    )
    (commitments_dir / "c.yaml").write_text(
        "status: fulfilled\nobserved: \"2026-01-01\"\n", encoding="utf-8"
    )

    result = _mod.cmd_improvement_queue(repo_root)

    assert result["commitments"]["present"] is True
    assert result["commitments"]["open_count"] == 2
    assert result["commitments"]["oldest_days"] is not None
    assert result["commitments"]["oldest_days"] >= 0


# ---------------------------------------------------------------------------
# push-failures
# ---------------------------------------------------------------------------

def test_push_failures_delegates_to_engine_op(tmp_path, monkeypatch):
    called_with = {}

    def _fake_surface(repo_root: str):
        called_with["repo_root"] = repo_root
        return {"total": 3, "recent_24h": 1, "last_line": "[..] PUSH FAILED"}

    monkeypatch.setattr(
        "coordinator_core.ops.workday_surface_auto_push_failure_stats.surface_auto_push_failure_stats",
        _fake_surface,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    result = _mod.cmd_push_failures(repo_root)

    assert result == {"total": 3, "recent_24h": 1, "last_line": "[..] PUSH FAILED", "error": None}
    assert called_with["repo_root"] == str(repo_root)


def test_push_failures_engine_error_degrades_to_error_field(tmp_path, monkeypatch):
    from coordinator_core.ops.workday_surface_auto_push_failure_stats import (
        PushFailureStatsError,
    )

    def _raise(_repo_root: str):
        raise PushFailureStatsError("boom")

    monkeypatch.setattr(
        "coordinator_core.ops.workday_surface_auto_push_failure_stats.surface_auto_push_failure_stats",
        _raise,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    result = _mod.cmd_push_failures(repo_root)

    assert result["total"] == 0
    assert result["recent_24h"] == 0
    assert result["last_line"] is None
    assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# local-ahead
# ---------------------------------------------------------------------------

def test_local_ahead_ineligible_branch_skips_git_network_calls(tmp_path):
    _init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "--quiet", "-b", "main"], cwd=str(tmp_path), check=True)

    result = _mod.cmd_local_ahead(tmp_path, do_fetch=True)

    assert result["branch"] == "main"
    assert result["eligible"] is False
    assert result["ahead_count"] == 0
    assert result["no_origin"] is False


def test_local_ahead_no_origin_counts_all_local_commits(tmp_path):
    _init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "--quiet", "-b", "work/testmachine/2026-07-23"], cwd=str(tmp_path), check=True)
    (tmp_path / "file2.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "add", "file2.txt"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "second"], cwd=str(tmp_path), check=True)

    result = _mod.cmd_local_ahead(tmp_path, do_fetch=False)

    assert result["branch"] == "work/testmachine/2026-07-23"
    assert result["eligible"] is True
    assert result["no_origin"] is True
    assert result["ahead_count"] == 2  # seed commit + second commit


def test_local_ahead_ahead_of_origin_counts_delta(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--quiet", "--bare"], cwd=str(origin), check=True)

    work = tmp_path / "work"
    work.mkdir()
    _init_git_repo(work)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(work), check=True)
    subprocess.run(["git", "checkout", "--quiet", "-b", "work/testmachine/2026-07-23"], cwd=str(work), check=True)
    subprocess.run(
        ["git", "push", "--quiet", "-u", "origin", "work/testmachine/2026-07-23"],
        cwd=str(work),
        check=True,
    )

    (work / "file2.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "add", "file2.txt"], cwd=str(work), check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "second"], cwd=str(work), check=True)

    result = _mod.cmd_local_ahead(work, do_fetch=True)

    assert result["branch"] == "work/testmachine/2026-07-23"
    assert result["eligible"] is True
    assert result["no_origin"] is False
    assert result["ahead_count"] == 1
    assert result["fetch_error"] is None


# ---------------------------------------------------------------------------
# stale-stashes (AC5)
# ---------------------------------------------------------------------------

def test_stale_stashes_delegates_to_engine_op(tmp_path, monkeypatch):
    called_with = {}

    def _fake_surface(repo_root: str, threshold_days: int = 7):
        called_with["repo_root"] = repo_root
        called_with["threshold_days"] = threshold_days
        return {
            "threshold_days": threshold_days,
            "total": 2,
            "stale": [{"ref": "stash@{0}", "age_days": 10, "subject": "On main: x"}],
            "advice": "inspect then scope",
            "error": None,
        }

    monkeypatch.setattr(
        "coordinator_core.ops.workday_surface_stale_stash_entries.surface_stale_stash_entries",
        _fake_surface,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    result = _mod.cmd_stale_stashes(repo_root, threshold_days=7)

    assert result["total"] == 2
    assert len(result["stale"]) == 1
    assert called_with == {"repo_root": str(repo_root), "threshold_days": 7}


def test_stale_stashes_engine_error_degrades_to_error_field(tmp_path, monkeypatch):
    from coordinator_core.ops.workday_surface_stale_stash_entries import (
        StaleStashEntriesError,
    )

    def _raise(_repo_root: str, threshold_days: int = 7):
        raise StaleStashEntriesError("boom")

    monkeypatch.setattr(
        "coordinator_core.ops.workday_surface_stale_stash_entries.surface_stale_stash_entries",
        _raise,
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    result = _mod.cmd_stale_stashes(repo_root, threshold_days=7)

    assert result["total"] == 0
    assert result["stale"] == []
    assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# CLI plumbing — end-to-end via main(), stdout is one JSON line, always exit 0
# ---------------------------------------------------------------------------

def test_main_improvement_queue_prints_one_json_line_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "coordinator_core.state_root.coordinator_state_root_central",
        lambda: "",
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    rc = _mod.main(["improvement-queue", "--repo-root", str(repo_root)])
    out = capsys.readouterr().out

    assert rc == 0
    lines = out.strip("\n").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert "central" in payload and "local" in payload and "recurring" in payload


def test_main_never_raises_on_unexpected_exception(tmp_path, monkeypatch, capsys):
    def _boom(_repo_root, do_fetch):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_mod, "cmd_local_ahead", _boom)

    rc = _mod.main(["local-ahead", "--repo-root", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out.strip())
    assert "kaboom" in payload["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
