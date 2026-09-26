
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

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
    that can fail a commit is a worse defect than the drift it looks for.

    Against the unwrapped handler this raises `RuntimeError` straight out of
    `_handler` and the commit never lands -- confirmed by inspection of the
    pre-fix code (no try/except around the two EOL calls), which is exactly
    what review finding 1 flagged: this test previously asserted
    `pytest.raises(RuntimeError)`, pinning the bug as the spec. The fix wraps
    both calls and degrades to no drift/no repair on any exception, so the
    commit must succeed here and carry no EOL warning.
    """
    repo = _repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"@echo off\necho changed\n")

    def boom(*_a, **_k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(commit_v2, "repair_declared_eol_drift", boom)

    result = _call(repo, {"paths": ["run.cmd"], "message": "launcher: edit"})

    assert result["committed"] is True
    assert result["warnings"] == []


def test_repair_skips_a_prefer_staged_path_even_when_drifted(tmp_path):
    repo = _repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"@echo off\necho changed\n")
    _git(["add", "--", "run.cmd"], repo)
    (repo / "run.cmd").write_bytes(b"@echo off\necho worktree edit\n")

    result = _call(
        repo,
        {
            "paths": ["run.cmd"],
            "message": "launcher: edit",
            "prefer_staged": ["run.cmd"],
        },
    )

    assert result["committed"] is True
    assert not any("run.cmd" in w for w in result["warnings"])
    assert b"\r\n" not in (repo / "run.cmd").read_bytes()
