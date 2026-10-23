"""review_brightline_gate default range in a repo with no origin remote."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_brightline_gate import _resolve_range
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


def _init(repo: Path, branch: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_no_origin_falls_back_to_local_main(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init(repo, "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(repo)

    range_, session, rc = _resolve_range(["--session-id", "sid-1"])

    assert rc == 0
    assert range_ == f"{base}..HEAD"
    assert session == "sid-1"


def test_no_origin_falls_back_to_master(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base = _init(repo, "master")
    _git(repo, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(repo)

    range_, _, rc = _resolve_range(["--session-id", "sid-1"])

    assert rc == 0
    assert range_ == f"{base}..HEAD"


def test_no_trunk_at_all_still_refuses(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _init(repo, "trunkless")
    monkeypatch.chdir(repo)

    range_, _, rc = _resolve_range(["--session-id", "sid-1"])

    assert rc == 1
    assert range_ is None
    assert "pass a range explicitly" in capsys.readouterr().err
