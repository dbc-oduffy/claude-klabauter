"""Forged-state regression suite for `directives_commit_tail.run_close_commit`
when a concurrent archive moved a declared stage path before the close commit.

The post-race state is forged in-process against a real git fixture repo: a
handoff is committed, then moved under `archive/` and the move committed, as a
peer archiver would.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_vanished_stage_path.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_commit_tail
from coordinator_core.session import record_homes

# Declared, not excused: the property under test is git's own commit result.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_HANDOFF = Path(record_homes.record_path("", "handoffs", "X.md")).as_posix()
_ARCHIVED = "archive/handoffs/2026-09/X.md"
_DIAGNOSTIC = "stage path vanished before commit: " + _HANDOFF


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    Path(record_homes.home_dir(str(root), "handoffs")).mkdir(parents=True)
    (root / _HANDOFF).write_text("handoff\n", encoding="utf-8")
    _git("add", "seed.txt", _HANDOFF, cwd=root)
    if _git("commit", "-qm", "seed", cwd=root).returncode != 0:
        pytest.skip("git unavailable — cannot build a fixture repo with history")
    (root / "archive/handoffs/2026-09").mkdir(parents=True)
    _git("mv", _HANDOFF, _ARCHIVED, cwd=root)
    _git("commit", "-qm", "peer archives the handoff", cwd=root)
    return root


def _close(root: Path, stage_paths):
    return directives_commit_tail.run_close_commit(
        root,
        session_id="test-session-vanished",
        subject="close after a concurrent archive",
        stage_paths=stage_paths,
    )


def _vanished_diagnostics(result):
    return [d for d in result.diagnostics if d.startswith(_DIAGNOSTIC)]


def test_partial_vanish_commits_present_paths_and_names_the_vanished_one(repo):
    (repo / "seed.txt").write_text("edited\n", encoding="utf-8")

    result = _close(repo, ["seed.txt", _HANDOFF])

    assert result.committed_sha is not None, result.diagnostics
    assert result.commit_failed is False
    shown = _git("show", f"{result.committed_sha}:seed.txt", cwd=repo)
    assert shown.stdout == "edited\n"
    assert len(_vanished_diagnostics(result)) == 1, result.diagnostics


def test_whole_vanish_fails_the_commit_and_names_the_vanished_path(repo):
    result = _close(repo, [_HANDOFF])

    assert result.commit_failed is True
    assert result.committed_sha is None
    assert len(_vanished_diagnostics(result)) >= 1, result.diagnostics


def test_empty_pathspec_is_a_benign_noop_without_diagnostic(repo):
    result = _close(repo, [])

    assert result.commit_failed is False
    assert result.committed_sha is None
    assert result.diagnostics == []
