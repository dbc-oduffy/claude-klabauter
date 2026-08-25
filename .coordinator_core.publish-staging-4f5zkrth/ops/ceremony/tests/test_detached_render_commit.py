"""
coordinator_core.ops.ceremony.tests.test_detached_render_commit

Tests for detached_render_commit.py -- the shared "stage + commit exactly one
path, bounded retry on `.git/index.lock` contention, log on exhaustion" helper
both detached renders (render-handoff-tracker.py, refresh-roadmap-callout.py)
use to own their artifact end-to-end (C5 residue, 2026-07-23 wsc-tail-slim-down).

Coverage:
  (a) commits_new_file           -- a genuinely dirty single path is staged and
                                     committed with the given message, explicit
                                     pathspec only.
  (b) noop_when_clean            -- a path with no diff against HEAD is a clean
                                     success (True), no commit created.
  (c) retries_lock_then_succeeds -- `.git/index.lock`-shaped stderr on the first
                                     N attempts, success on a later one within
                                     budget -- no sleep-related hang, retried
                                     attempts consumed correctly.
  (d) exhausts_retries_and_logs  -- persistent lock contention across every
                                     attempt -- returns False, exactly
                                     `_MAX_ATTEMPTS` attempts made, one CHILD
                                     FAILED record appended to the shared log.
  (e) nonlock_failure_no_retry   -- a non-lock git failure is terminal on the
                                     FIRST attempt -- no retry burned, one CHILD
                                     FAILED record logged immediately.

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C5 (Artifact
disposition residue)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops.ceremony import detached_render_commit as drc

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed"],
        cwd=str(root), check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    return tmp_path


def _log_subjects(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# (a) real single-path commit
# ---------------------------------------------------------------------------


def test_commits_new_file(repo: Path) -> None:
    target = repo / "state" / "handoff-tracker.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Handoff Tracker\n", encoding="utf-8")

    ok = drc.commit_own_artifact(
        repo, "state/handoff-tracker.md", "tracker: refresh handoff-tracker.md",
        caller_label="test:commits_new_file",
    )

    assert ok is True
    subjects = _log_subjects(repo)
    assert subjects[0] == "tracker: refresh handoff-tracker.md"
    # Explicit pathspec only -- nothing else staged/committed.
    show = subprocess.run(
        ["git", "show", "--name-only", "--format="], cwd=str(repo),
        capture_output=True, text=True, check=True,
    )
    assert show.stdout.strip() == "state/handoff-tracker.md"


# ---------------------------------------------------------------------------
# (b) no-op on a clean path
# ---------------------------------------------------------------------------


def test_noop_when_clean(repo: Path) -> None:
    target = repo / "state" / "handoff-tracker.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Handoff Tracker\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "state/handoff-tracker.md"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "pre-existing"],
        cwd=str(repo), check=True,
    )
    before = _log_subjects(repo)

    ok = drc.commit_own_artifact(
        repo, "state/handoff-tracker.md", "tracker: refresh handoff-tracker.md",
        caller_label="test:noop_when_clean",
    )

    assert ok is True
    assert _log_subjects(repo) == before  # no new commit landed

    log_path = repo / "state" / "housekeeping-failures.log"
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# (c)/(d)/(e) retry/backoff mechanics (mocked subprocess -- no real git needed)
# ---------------------------------------------------------------------------


def _fake_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout="", stderr=stderr)


def test_retries_lock_then_succeeds(tmp_path: Path) -> None:
    # add: lock, lock, ok -> diff: dirty (1) -> commit: ok
    responses = [
        _fake_completed(128, "fatal: Unable to create '.git/index.lock': File exists."),
        _fake_completed(128, "fatal: Unable to create '.git/index.lock': File exists."),
        _fake_completed(0),   # git add succeeds
        _fake_completed(1),   # git diff --cached --quiet: dirty
        _fake_completed(0),   # git commit succeeds
    ]

    with mock.patch.object(drc, "_run_git", side_effect=responses) as mock_run, \
         mock.patch.object(drc.time, "sleep") as mock_sleep:
        ok = drc.commit_own_artifact(
            tmp_path, "some/path.md", "msg", caller_label="test:retries_lock_then_succeeds",
        )

    assert ok is True
    assert mock_run.call_count == 5
    assert mock_sleep.call_count == 2  # slept before the 2nd and 3rd attempts only
    log_path = tmp_path / "state" / "housekeeping-failures.log"
    assert not log_path.exists()


def test_exhausts_retries_and_logs(tmp_path: Path) -> None:
    lock_failure = _fake_completed(128, "fatal: Unable to create '.git/index.lock': File exists.")

    with mock.patch.object(drc, "_run_git", return_value=lock_failure) as mock_run, \
         mock.patch.object(drc.time, "sleep") as mock_sleep:
        ok = drc.commit_own_artifact(
            tmp_path, "some/path.md", "msg", caller_label="test:exhausts_retries_and_logs",
        )

    assert ok is False
    assert mock_run.call_count == drc._MAX_ATTEMPTS  # one `git add` attempt per retry cycle
    assert mock_sleep.call_count == drc._MAX_ATTEMPTS - 1

    log_path = tmp_path / "state" / "housekeeping-failures.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "CHILD FAILED" in content
    assert "test:exhausts_retries_and_logs" in content
    assert "index.lock" in content


def test_nonlock_failure_no_retry(tmp_path: Path) -> None:
    hard_failure = _fake_completed(1, "fatal: unable to write new_index file")

    with mock.patch.object(drc, "_run_git", return_value=hard_failure) as mock_run, \
         mock.patch.object(drc.time, "sleep") as mock_sleep:
        ok = drc.commit_own_artifact(
            tmp_path, "some/path.md", "msg", caller_label="test:nonlock_failure_no_retry",
        )

    assert ok is False
    assert mock_run.call_count == 1  # terminal on first sight -- no retry burned
    mock_sleep.assert_not_called()

    log_path = tmp_path / "state" / "housekeeping-failures.log"
    content = log_path.read_text(encoding="utf-8")
    assert "CHILD FAILED" in content
    assert "unable to write new_index file" in content
