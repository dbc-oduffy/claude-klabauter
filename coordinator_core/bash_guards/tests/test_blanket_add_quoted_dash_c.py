"""A quoted `git -C <repo>` path must not hide a blanket add or an all-tracked commit from its deny."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_git_shared, dispatch_checks
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_passthrough_kwargs


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, **no_console_passthrough_kwargs())


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("x")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "base.txt").write_text("edited")
    return repo


def _verdict(cmd: str) -> str:
    for check in (dispatch_checks.check_blanket_git_add, dispatch_checks.check_git_commit_safe_commit_advise):
        out = envelope_of(check(cmd, "sess-quoted-dash-c"))
        if out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny":
            return json.dumps(out)
    return ""


def _quote(style: str, path: Path) -> str:
    q = '"' if style == "double" else "'"
    return f"{q}{path}{q}"


@pytest.mark.spawns_process
@pytest.mark.parametrize("repo_name", ["repo", "my repo"])
@pytest.mark.parametrize("style", ["double", "single"])
@pytest.mark.parametrize("tail", ["add -A", "add .", "add --all", "commit -am x"])
def test_quoted_dash_c_blanket_forms_deny(tmp_path, monkeypatch, repo_name, style, tail):
    repo = _repo(tmp_path, repo_name)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    text = _verdict(f"git -C {_quote(style, repo)} {tail}")
    assert text
    assert "ceremony.commit_v2" in text


@pytest.mark.spawns_process
@pytest.mark.parametrize("style", ["double", "single"])
def test_quoted_dash_c_scoped_add_still_passes(tmp_path, monkeypatch, style):
    repo = _repo(tmp_path, "my repo")
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    assert _verdict(f"git -C {_quote(style, repo)} add -- base.txt") == ""
