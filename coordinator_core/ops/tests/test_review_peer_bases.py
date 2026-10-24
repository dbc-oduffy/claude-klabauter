"""peer_path_bases: per-declared-path effective base from Deliverable-Id / Session-Id trailers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_peer_bases as rpb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
DELIVERABLE = "dlv-own"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("a = 0\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(repo: Path, name: str, text: str, session=None, deliverable=None) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    msg = f"edit {name}\n\n"
    if session:
        msg += f"Session-Id: {session}\n"
    if deliverable:
        msg += f"Deliverable-Id: {deliverable}\n"
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _run(repo, base, paths, deliverable=DELIVERABLE):
    return rpb.peer_path_bases(repo, base, None, paths, SESSION, deliverable)


def test_empty_paths_spawns_nothing(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rpb, "run_git", boom)
    assert rpb.peer_path_bases(repo, "HEAD", None, [], SESSION, DELIVERABLE) == {}


def test_no_peer_commit_returns_empty(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n")
    assert _run(repo, base, ["a.py"]) == {}


@pytest.mark.parametrize("peer_session,peer_deliverable", [("sess-peer", None), (SESSION, "dlv-peer")])
def test_peer_then_own_maps_path(repo, peer_session, peer_deliverable):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", peer_session, peer_deliverable)
    _commit(repo, "a.py", "a = 2\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": peer}


@pytest.mark.parametrize("peer_session,peer_deliverable", [("sess-peer", None), (SESSION, "dlv-peer")])
def test_own_then_peer_omits_path(repo, peer_session, peer_deliverable):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n", peer_session, peer_deliverable)
    assert _run(repo, base, ["a.py"]) == {}


def test_latest_peer_before_own_wins(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-p1")
    second = _commit(repo, "a.py", "a = 2\n", "sess-p2")
    _commit(repo, "a.py", "a = 3\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": second}


def test_trailerless_commit_is_own(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n")
    assert _run(repo, base, ["a.py"]) == {}


def test_own_deliverable_under_foreign_session_is_peer(repo):
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-other", DELIVERABLE)
    _commit(repo, "a.py", "a = 2\n", SESSION, DELIVERABLE)
    assert _run(repo, base, ["a.py"]) == {"a.py": peer}


def test_no_own_deliverable_uses_session_only(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", SESSION, "dlv-peer")
    assert _run(repo, base, ["a.py"], deliverable=None) == {}


def test_paths_are_independent(repo):
    (repo / "b.py").write_text("b = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b base")
    base = _git(repo, "rev-parse", "HEAD")
    peer = _commit(repo, "a.py", "a = 1\n", "sess-peer")
    _commit(repo, "b.py", "b = 1\n", SESSION, DELIVERABLE)
    _commit(repo, "b.py", "b = 2\n", "sess-peer")
    assert _run(repo, base, ["a.py", "b.py"]) == {"a.py": peer}


def test_exactly_one_git_spawn(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "a.py", "a = 1\n", "sess-peer")
    calls = []
    real = rpb.run_git

    def counting(args, **kw):
        calls.append(args)
        return real(args, **kw)

    monkeypatch.setattr(rpb, "run_git", counting)
    assert _run(repo, base, ["a.py"])
    assert len(calls) == 1
