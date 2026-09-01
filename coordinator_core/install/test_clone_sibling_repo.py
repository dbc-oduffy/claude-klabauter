"""Tests for coordinator_core.install.clone_sibling_repo.

All git invocations here run against throwaway repos created fresh under
pytest's `tmp_path` fixture — never the working claude-klabauter repo. The
source repo is a plain (non-bare) local checkout with one commit, cloned via
`clone_idempotent()`'s own `git clone` subprocess into a sibling `tmp_path`
directory, exercising the real git binary end-to-end rather than mocking it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.install.clone_sibling_repo import (
    CloneSiblingRepoError,
    _clone_idempotent,
    clone_idempotent,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
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
    """A throwaway local git repo (one commit) to clone from — lives entirely
    under tmp_path, never touches the working repo."""
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
    """AC7 — double-invocation with identical inputs is idempotent: the
    second call must not re-clone, must not error, and must not mutate the
    already-present checkout."""
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
    """A target dir with a genuine `.git` directory and `origin` set to
    `remote_url` — simulates an already-cloned target for the already-present
    branch, distinct from `_make_source_repo`'s role as the clone source."""
    target = tmp_path / name
    target.mkdir()
    _run_git(["init"], cwd=target)
    _run_git(["remote", "add", "origin", remote_url], cwd=target)
    return target


def test_already_present_with_matching_remote_short_circuits(tmp_path):
    """AC — the already-present branch adopts the target only after
    confirming its `origin` remote matches `repo_url`."""
    target = _make_existing_clone(tmp_path, "https://example.invalid/repo.git")

    result = clone_idempotent("https://example.invalid/repo.git", str(target))

    assert result == {"cloned": False, "already_present": True, "path": str(target)}


def test_already_present_remote_url_normalizes_trailing_git_and_slash(tmp_path):
    target = _make_existing_clone(tmp_path, "https://example.invalid/repo.git")

    result = clone_idempotent("https://example.invalid/repo/", str(target))

    assert result == {"cloned": False, "already_present": True, "path": str(target)}


def test_already_present_with_mismatched_remote_raises_loudly(tmp_path):
    """AC — a `.git` directory whose `origin` points elsewhere is refused,
    not silently adopted (the C10 wrong-repo hazard)."""
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


def test_registered_handler_dispatches_to_clone_idempotent(tmp_path):
    src = _make_source_repo(tmp_path)
    target = tmp_path / "cloned" / "via-handler"

    # Review: code-reviewer — Finding 1. Handler is a plain sync `def`
    # (engine auto-offloads via asyncio.to_thread), called directly rather
    # than via asyncio.run.
    result = _clone_idempotent({"repo_url": str(src), "target_dir": str(target)})

    assert result == {"cloned": True, "already_present": False, "path": str(target)}


def test_registered_handler_requires_both_params():
    with pytest.raises(ValueError):
        _clone_idempotent({"repo_url": "x"})
    with pytest.raises(ValueError):
        _clone_idempotent({"target_dir": "y"})


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — this writer's sole ShapedClause (index 0).
# ---------------------------------------------------------------------------


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
