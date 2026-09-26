from __future__ import annotations

import subprocess
from unittest import mock
from pathlib import Path

import pytest

from coordinator_core.install.clone_sibling_repo import (
    CloneSiblingRepoError,
    _clone_idempotent,
    clone_idempotent,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GIT_TIMEOUT = 30


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=False,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _make_source_repo(tmp_path: Path) -> Path:
    src = tmp_path / "source-repo"
    src.mkdir()
    _run_git(["init"], cwd=src)
    _run_git(["config", "user.email", "test@example.invalid"], cwd=src)
    _run_git(["config", "user.name", "Test"], cwd=src)
    (src / "README.md").write_text("hello\n")
    _run_git(["add", "README.md"], cwd=src)
    _run_git(["commit", "-m", "initial"], cwd=src)
    return src


def test_fresh_clone_reports_cloned_true(tmp_path):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    result = clone_idempotent(str(src), str(target))

    assert result == {"cloned": True, "already_present": False, "path": str(target)}
    assert (target / ".git").is_dir()
    assert (target / "README.md").is_file()


def test_second_invocation_is_a_safe_no_op(tmp_path):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    first = clone_idempotent(str(src), str(target))
    assert first["cloned"] is True

    marker = target / "README.md"
    original_mtime = marker.stat().st_mtime

    second = clone_idempotent(str(src), str(target))

    assert second == {"cloned": False, "already_present": True, "path": str(target)}
    assert marker.stat().st_mtime == original_mtime


def _make_existing_clone(tmp_path: Path, remote_url: str, name: str = "existing") -> Path:
    target = tmp_path / name
    target.mkdir()
    _run_git(["init"], cwd=target)
    _run_git(["remote", "add", "origin", remote_url], cwd=target)
    return target


def test_already_present_with_matching_remote_short_circuits(tmp_path):
    target = _make_existing_clone(tmp_path, "https://example.invalid/repo.git")

    result = clone_idempotent("https://example.invalid/repo.git", str(target))

    assert result == {"cloned": False, "already_present": True, "path": str(target)}


def test_already_present_remote_url_normalizes_trailing_git_and_slash(tmp_path):
    target = _make_existing_clone(tmp_path, "https://example.invalid/repo.git")

    result = clone_idempotent("https://example.invalid/repo/", str(target))

    assert result == {"cloned": False, "already_present": True, "path": str(target)}


def test_already_present_with_mismatched_remote_raises_loudly(tmp_path):
    target = _make_existing_clone(tmp_path, "https://example.invalid/wrong-repo.git")

    with pytest.raises(CloneSiblingRepoError):
        clone_idempotent("https://example.invalid/right-repo.git", str(target))


def test_already_present_with_no_origin_remote_raises_loudly(tmp_path):
    target = tmp_path / "existing-no-remote"
    target.mkdir()
    _run_git(["init"], cwd=target)

    with pytest.raises(CloneSiblingRepoError):
        clone_idempotent("https://example.invalid/repo.git", str(target))


def test_clone_failure_raises_clone_sibling_repo_error(tmp_path):
    nonexistent_source = tmp_path / "does-not-exist"
    target = tmp_path / "target"

    with pytest.raises(CloneSiblingRepoError):
        clone_idempotent(str(nonexistent_source), str(target))

    assert not (target / ".git").is_dir()


def test_missing_git_executable_raises_clone_sibling_repo_error(tmp_path, monkeypatch):
    target = tmp_path / "target"

    def _missing(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _missing)

    with pytest.raises(CloneSiblingRepoError):
        clone_idempotent("https://example.invalid/repo.git", str(target))


def test_already_present_origin_read_with_git_missing_raises_not_none(tmp_path):
    target = tmp_path / "existing-git-missing"
    target.mkdir()
    _run_git(["init"], cwd=target)

    real_run = subprocess.run

    def _no_git(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if argv and argv[0] == "git" and "get-url" in argv:
            raise FileNotFoundError("git not found")
        return real_run(*args, **kwargs)

    with mock.patch.object(subprocess, "run", _no_git):
        with pytest.raises(CloneSiblingRepoError):
            clone_idempotent("https://example.invalid/repo.git", str(target))


def test_already_present_origin_read_timeout_raises_not_none(tmp_path):
    target = tmp_path / "existing-origin-timeout"
    target.mkdir()
    _run_git(["init"], cwd=target)

    real_run = subprocess.run

    def _timeout(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if argv and argv[0] == "git" and "get-url" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        return real_run(*args, **kwargs)

    with mock.patch.object(subprocess, "run", _timeout):
        with pytest.raises(CloneSiblingRepoError):
            clone_idempotent("https://example.invalid/repo.git", str(target))


def test_registered_handler_dispatches_to_clone_idempotent(tmp_path):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "via-handler"

    result = _clone_idempotent({"repo_url": str(src), "target_dir": str(target)})

    assert result == {"cloned": True, "already_present": False, "path": str(target)}


def test_registered_handler_requires_both_params():
    with pytest.raises(ValueError):
        _clone_idempotent({"repo_url": "x"})
    with pytest.raises(ValueError):
        _clone_idempotent({"target_dir": "y"})


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("clone-sibling-repo", {}).get(0)


def test_journal_records_fresh_clone(tmp_path, _journal_env):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "sibling"

    clone_idempotent(str(src), str(target))

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert [e.path for e in resolution.entries] == [str(target)]
    assert resolution.entries[0].kind == "file-path"


def test_journal_records_already_present_target(tmp_path, _journal_env):
    target = _make_existing_clone(tmp_path, "https://example.invalid/repo.git")

    clone_idempotent("https://example.invalid/repo.git", str(target))

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert [e.path for e in resolution.entries] == [str(target)]


def test_journal_unreported_on_clone_failure(tmp_path, _journal_env):
    nonexistent_source = tmp_path / "does-not-exist"
    target = tmp_path / "target"

    with pytest.raises(CloneSiblingRepoError):
        clone_idempotent(str(nonexistent_source), str(target))

    assert _resolved(_journal_env) is None
