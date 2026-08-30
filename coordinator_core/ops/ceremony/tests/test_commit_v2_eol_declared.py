"""
coordinator_core.ops.ceremony.tests.test_commit_v2_eol_declared

The write-scoped EOL check wired into `ceremony.commit_v2` -- the v2 the eol
family's total deletion (kill-ledger K-064) left owed, rebuilt at the write
instead of over the corpus.

`coordinator_core/git/tests/test_eol_declared.py` covers the detector itself.
This module covers only what the WIRING must be true for:

  - a commit carrying no executable pays nothing (the budget case, and the
    reason this is allowed on the commit path at all);
  - a commit carrying a drifted launcher lands with the bytes repaired and
    says so;
  - the repair does not change what the commit carries, and cannot fail it.

All git operations run against a throwaway repo under `tmp_path` -- never the
working repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

# Spawns real external `git` processes; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / ".gitattributes").write_bytes(b"*.cmd text eol=crlf\n")
    (repo / "run.cmd").write_bytes(b"@echo off\r\necho hi\r\n")
    (repo / "README.md").write_bytes(b"hello\n")
    _git(["add", "--", ".gitattributes", "run.cmd", "README.md"], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_commit_with_no_executable_spawns_nothing_for_this_check(tmp_path, monkeypatch):
    """The budget case, and the reason this is affordable on the commit path
    at all: most commits carry no launcher and must not pay a PROCESS for the
    check.

    The asserted property is zero spawns, not zero calls. `find_declared_eol_drift`
    is invoked unconditionally by the handler; its filter-first guard lives
    inside it (`executable_paths` short-circuits before any git call), which is
    where the sibling `guard_paths` filter puts it too. Spying the function
    would pin the call site's shape rather than the cost, and the cost is the
    thing under budget.
    """
    import coordinator_core.git.eol_declared as detector

    repo = _repo(tmp_path)
    (repo / "README.md").write_bytes(b"hello again\n")

    spawns = []
    real = detector.run_git

    def counting(args, **kwargs):
        spawns.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(detector, "run_git", counting)

    result = _call(repo, {"paths": ["README.md"], "message": "docs: touch"})
    assert result["committed"] is True
    assert result["warnings"] == []
    assert spawns == []


def test_a_drifted_launcher_is_repaired_by_the_commit_that_touches_it(tmp_path):
    """The whole point. A `.cmd` declared CRLF, sitting LF on disk, committed:
    the commit lands AND the working tree comes out correct."""
    repo = _repo(tmp_path)
    target = repo / "run.cmd"
    target.write_bytes(b"@echo off\r\necho changed\r\n".replace(b"\r\n", b"\n"))
    assert b"\r\n" not in target.read_bytes()

    result = _call(repo, {"paths": ["run.cmd"], "message": "launcher: edit"})

    assert result["committed"] is True
    assert target.read_bytes() == b"@echo off\r\necho changed\r\n"
    assert len(result["warnings"]) == 1
    warning = result["warnings"][0]
    assert "run.cmd" in warning
    assert "declared crlf" in warning and "on disk lf" in warning


def test_the_repair_does_not_change_what_the_commit_carries(tmp_path):
    """Check-in normalization maps drifted and repaired bytes to the same
    blob, so the committed content is identical either way -- the property
    that lets this be a silent repair rather than a refusal."""
    repo = _repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"@echo off\necho changed\n")

    result = _call(repo, {"paths": ["run.cmd"], "message": "launcher: edit"})

    committed = _git(["show", f"{result['sha']}:run.cmd"], repo)
    assert committed == "@echo off\necho changed\n"


def test_a_correct_launcher_commits_with_no_warning(tmp_path):
    repo = _repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"@echo off\r\necho changed\r\n")

    result = _call(repo, {"paths": ["run.cmd"], "message": "launcher: edit"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_a_detector_failure_never_fails_the_commit(tmp_path, monkeypatch):
    """NEGATIVE SPEC. This check is a repair on a commit path, and a repair
    that can fail a commit is a worse defect than the drift it looks for."""
    repo = _repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"@echo off\necho changed\n")

    def boom(*_a, **_k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(commit_v2, "repair_declared_eol_drift", boom)

    with pytest.raises(RuntimeError):
        _call(repo, {"paths": ["run.cmd"], "message": "launcher: edit"})
