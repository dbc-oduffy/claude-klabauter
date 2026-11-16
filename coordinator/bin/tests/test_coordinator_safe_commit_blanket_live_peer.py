"""`--blanket` withholds staged paths written after a live peer started; older orphans still land."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess

import pytest
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_SELF = "self-session"
_PEER = "peer-session"
_PEER_START = "2020-01-01T00:00:00Z"
_PEER_START_EPOCH = 1577836800
_ORPHAN_MTIME = 1262304000  # 2010-01-01


def _load_cli_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / ".git" / "info" / "exclude").write_text(".coordinator-local/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SELF)
    monkeypatch.delenv("COORDINATOR_BLANKET_ACCEPT_FOREIGN", raising=False)
    monkeypatch.delenv("CLAUDE_INVOKING_COMMAND", raising=False)
    return tmp_path


def _arm_live_peer(repo, monkeypatch, started_at: str) -> None:
    from coordinator_core.session import core as cs_core
    from coordinator_core.session import liveness as cs_liveness

    peer_dir = pathlib.Path(cs_core.sessions_dir()) / _PEER
    peer_dir.mkdir(parents=True, exist_ok=True)
    (peer_dir / "started_at").write_text(started_at + "\n", encoding="utf-8")
    monkeypatch.setattr(cs_liveness, "live_session_ids", lambda cwd=None: frozenset({_SELF, _PEER}))


def _write(repo, name, mtime=None) -> None:
    p = repo / name
    p.write_text(name, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _run_blanket(mod):
    """Drive the real CLI entry; a normal return is exit 0."""
    try:
        mod.main(["--blanket", "--invoking-command", "update-docs", "sweep"])
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _committed(repo) -> list[str]:
    return _git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split()


def test_path_written_after_a_live_peer_started_is_withheld_and_orphan_lands(repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_cli_module()
    _arm_live_peer(repo, monkeypatch, _PEER_START)
    _write(repo, "orphan.md", _ORPHAN_MTIME)
    _write(repo, "peer-dirty.md")

    assert _run_blanket(mod) == 0

    assert _committed(repo) == ["orphan.md"]
    assert "peer-dirty.md" in _git(repo, "status", "--porcelain")
    err = capsys.readouterr().err
    assert "withheld 1 path(s) written after a live peer started" in err
    assert "peer-dirty.md" in err


def test_withhold_leaves_nothing_to_commit_when_every_path_is_a_peer_window_path(repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_cli_module()
    _arm_live_peer(repo, monkeypatch, _PEER_START)
    _write(repo, "peer-dirty.md")
    head = _git(repo, "rev-parse", "HEAD")

    assert _run_blanket(mod) == 0

    assert _git(repo, "rev-parse", "HEAD") == head
    assert "peer-dirty.md" in _git(repo, "status", "--porcelain")


def test_no_live_peer_commits_everything(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.session import liveness as cs_liveness

    mod = _load_cli_module()
    monkeypatch.setattr(cs_liveness, "live_session_ids", lambda cwd=None: frozenset({_SELF}))
    from coordinator_core.session import core as cs_core

    os.makedirs(cs_core.sessions_dir(), exist_ok=True)
    _write(repo, "a.md")

    assert _run_blanket(mod) == 0

    assert _committed(repo) == ["a.md"]


def test_accept_foreign_escape_hatch_skips_the_withhold(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_cli_module()
    monkeypatch.setenv("COORDINATOR_BLANKET_ACCEPT_FOREIGN", "1")
    _arm_live_peer(repo, monkeypatch, _PEER_START)
    _write(repo, "peer-dirty.md")

    assert _run_blanket(mod) == 0

    assert _committed(repo) == ["peer-dirty.md"]
