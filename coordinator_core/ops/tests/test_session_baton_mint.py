
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
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _mint(**params):
    return mint_mod._handler(dict(params))


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


def test_mint_spawns_no_subprocess(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-no-spawn")

    def _raise(*args, **kwargs):
        raise AssertionError(
            "session_baton.mint must not reach the git rev-parse spawn "
            f"fallback — got _spawn_rev_parse({args!r}, {kwargs!r})"
        )

    from coordinator_core.git import repo_root as _repo_root_mod

    monkeypatch.setattr(_repo_root_mod, "_spawn_rev_parse", _raise, raising=True)

    result = _mint(session_id="sid-no-spawn", prompt="hello", cwd=str(repo))
    assert result["exit_code"] == 0
    assert result["first_prompt"] == "hello"
