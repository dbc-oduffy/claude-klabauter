"""
coordinator_core.ops.tests.test_session_baton_mint — round-trip,
idempotency, first-prompt-wins, and the no-subprocess budget assertion for
the "session_baton.mint" op.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C2.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import session_baton_mint as mint_mod
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_passthrough_kwargs


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    """Pre-create the per-session directory ``cs_init`` mints on every real
    session start — this store (C6, docs/plans/2026-08-19-batons-unify-into-
    one-successor.md § C6) no longer mkdir's it itself, so a fixture calling
    the mint op directly (bypassing session init) must bring it into being."""
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _mint(**params):
    return mint_mod._handler(dict(params))


# ---------------------------------------------------------------------------
# Basic mint / round-trip
# ---------------------------------------------------------------------------


def test_mint_creates_record_with_first_prompt(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-1")
    result = _mint(session_id="sid-1", prompt="hello world", cwd=str(repo))

    assert result["exit_code"] == 0
    assert result["error"] is None
    assert result["session_id"] == "sid-1"
    assert result["created"] is True
    assert result["first_prompt"] == "hello world"
    assert result["baton_path"] == str(
        repo / ".git" / "coordinator-sessions" / "sid-1" / "baton.json"
    )

    on_disk = store.read_baton("sid-1", cwd=str(repo))
    assert on_disk["first_prompt"] == "hello world"
    assert on_disk["created_at"] is not None


def test_mint_without_prompt_still_creates_record(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-noprompt")
    result = _mint(session_id="sid-noprompt", cwd=str(repo))

    assert result["exit_code"] == 0
    assert result["created"] is True
    assert result["first_prompt"] is None


# ---------------------------------------------------------------------------
# Idempotency: second call updates, never duplicates the file
# ---------------------------------------------------------------------------


def test_second_call_same_session_updates_not_duplicates(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-idem")
    first = _mint(session_id="sid-idem", prompt="p1", cwd=str(repo))
    assert first["created"] is True

    second = _mint(session_id="sid-idem", prompt="p1", cwd=str(repo))
    assert second["created"] is False
    assert second["session_id"] == "sid-idem"

    baton_files = list(
        (repo / ".git" / "coordinator-sessions" / "sid-idem").glob("*.json")
    )
    assert len(baton_files) == 1


# ---------------------------------------------------------------------------
# First-prompt-wins: a later call never overwrites the first-captured prompt
# ---------------------------------------------------------------------------


def test_later_call_does_not_overwrite_first_prompt(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-first-wins")
    _mint(session_id="sid-first-wins", prompt="the real first prompt", cwd=str(repo))
    second = _mint(
        session_id="sid-first-wins", prompt="a later, different prompt", cwd=str(repo)
    )

    assert second["first_prompt"] == "the real first prompt"
    on_disk = store.read_baton("sid-first-wins", cwd=str(repo))
    assert on_disk["first_prompt"] == "the real first prompt"


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


def test_missing_session_id_errors():
    result = _mint(prompt="hi")
    assert result["exit_code"] == 1
    assert result["session_id"] is None
    assert "session_id" in result["error"]


def test_blank_session_id_errors():
    result = _mint(session_id="   ", prompt="hi")
    assert result["exit_code"] == 1


def test_non_string_prompt_errors(tmp_path):
    repo = _make_repo(tmp_path)
    result = _mint(session_id="sid-badtype", prompt=123, cwd=str(repo))
    assert result["exit_code"] == 1
    assert "prompt" in result["error"]


def test_non_git_cwd_errors(tmp_path):
    result = _mint(session_id="sid-nogit", prompt="hi", cwd=str(tmp_path))
    assert result["exit_code"] == 1
    assert result["session_id"] is None


# ---------------------------------------------------------------------------
# Budget: no subprocess spawned by this op's own code path
# ---------------------------------------------------------------------------


def test_mint_spawns_no_subprocess(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-no-spawn")

    def _raise(*args, **kwargs):
        raise AssertionError(
            "session_baton.mint must not reach the git rev-parse spawn "
            f"fallback — got _spawn_rev_parse({args!r}, {kwargs!r})"
        )

    # coordinator_core.git.repo_root is store's own git-common-dir resolver;
    # its walk-based resolution must succeed without ever reaching the
    # `git rev-parse` spawn-fallback for an ordinary on-disk repo (see that
    # module's docstring — `subprocess` is imported function-locally inside
    # `_spawn_rev_parse`, precisely so the common walk-only path never loads
    # it at all). Patching `_spawn_rev_parse` itself is therefore the correct
    # seam: it is the ONE function on this path that would import and call
    # `subprocess`, and patching it directly (rather than a module-level
    # `subprocess` attribute that does not exist until that function runs)
    # asserts the walk path is taken without depending on that import timing.
    from coordinator_core.git import repo_root as _repo_root_mod

    monkeypatch.setattr(_repo_root_mod, "_spawn_rev_parse", _raise, raising=True)

    result = _mint(session_id="sid-no-spawn", prompt="hello", cwd=str(repo))
    assert result["exit_code"] == 0
    assert result["first_prompt"] == "hello"
