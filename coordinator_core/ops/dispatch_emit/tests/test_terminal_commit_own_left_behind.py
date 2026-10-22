"""dispatch.terminal_commit's product commit is declared-only (carry off); the committing session's
own left-behind claims land in a follow-on commit attributed to it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import own_carry, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_DECLARED = "state/audits/a.yaml"
_DEBT = "state/debt-backlog/d.yaml"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    for rel in (_DECLARED, _DEBT):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x: 1\n", encoding="utf-8", newline="")
    session_scope.touch(SELF, _DECLARED, cwd=str(root))
    return root


@pytest.fixture
def seen(monkeypatch):
    """Records each commit_v2 call's params and lets the real op run."""
    calls: list = []
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, repo_root=None):
        if op == "ceremony.commit_v2":
            calls.append(dict(params))
        return real(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    return calls


def _run(repo: Path, **params) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(_DECLARED,)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF, **params},
        repo_root=repo / ".git",
    )


def _in_commit(repo: Path, sha: str) -> list:
    out = _git(["show", "--name-only", "--format=", sha], repo)
    return sorted(ln for ln in out.splitlines() if ln.strip())


def test_product_call_carries_off_and_follow_on_lands_the_claimed_leftover(repo, seen):
    session_scope.touch(SELF, _DEBT, cwd=str(repo))
    out = _run(repo)
    assert out["committed"] is True, out
    assert seen[0]["carry_own_left_behind"] is False
    assert _in_commit(repo, out["sha"]) != [] and _DEBT not in _in_commit(repo, out["sha"])
    follow = out[own_carry.REPLY_KEY]
    assert follow["paths"] == [_DEBT]
    assert _in_commit(repo, follow["sha"]) == [_DEBT]
    assert _git(["rev-parse", f"{follow['sha']}^"], repo).strip() == out["sha"]
    body = _git(["show", "-s", "--format=%B", follow["sha"]], repo)
    assert f"{own_carry.TRAILER}: {out['sha']}" in body
    assert _git(["status", "--porcelain", "-uall"], repo).count(_DEBT) == 0


def test_a_peer_held_leftover_is_in_neither_commit(repo, seen, monkeypatch):
    session_scope.touch(SELF, _DEBT, cwd=str(repo))
    real = session_scope.contested_by_live_peers
    monkeypatch.setattr(
        session_scope, "contested_by_live_peers",
        lambda paths, sid, cwd=None: {**(real(paths, sid, cwd=cwd) or {}), _DEBT: [PEER]},
    )
    out = _run(repo)
    assert out["committed"] is True, out
    assert _DEBT not in _in_commit(repo, out["sha"])
    assert own_carry.REPLY_KEY not in out
    assert len(seen) == 1
    assert "?? " + _DEBT in _git(["status", "--porcelain", "-uall"], repo)


def test_nothing_left_makes_no_follow_on_call(repo, seen):
    out = _run(repo)
    assert out["committed"] is True, out
    assert len(seen) == 1
    assert own_carry.REPLY_KEY not in out


def test_an_unanswerable_contest_carries_nothing(repo, seen, monkeypatch):
    session_scope.touch(SELF, _DEBT, cwd=str(repo))
    monkeypatch.setattr(session_scope, "contested_by_live_peers", lambda *a, **k: None)
    out = _run(repo)
    assert out["committed"] is True, out
    assert len(seen) == 1
    assert own_carry.REPLY_KEY not in out


def test_a_refused_follow_on_never_unlands_the_product_commit(repo, monkeypatch):
    session_scope.touch(SELF, _DEBT, cwd=str(repo))
    real = terminal_commit.reentrant_dispatch

    def spy(op, params, repo_root=None):
        if op == "ceremony.commit_v2" and params.get("carry_own_left_behind") is False \
                and params["paths"] == [_DEBT]:
            return {"committed": False, "error": "boom"}
        return real(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", spy)
    out = _run(repo)
    assert out["committed"] is True, out
    assert out[own_carry.REPLY_KEY] == {"error": "boom"}
    assert _in_commit(repo, out["sha"]) != []


def test_no_session_id_makes_no_follow_on(repo, seen):
    session_scope.touch(SELF, _DEBT, cwd=str(repo))
    out = _run(repo, session_id=None)
    assert own_carry.REPLY_KEY not in out
    assert all(c["carry_own_left_behind"] is False for c in seen)
