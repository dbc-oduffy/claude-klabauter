
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim_cleared(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid not in result.get(rel_path, [])


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def test_commit_releases_this_sessions_own_claim(repo, monkeypatch):
    sid = "commit-v2-claim-release-test"
    _own_sid(monkeypatch, sid)

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)
    f.write_text("v2\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert _claim_cleared(repo, sid, rel)


def test_peer_claimed_path_untouched(repo, monkeypatch):
    sid = "commit-v2-claim-release-self"
    peer_sid = "commit-v2-claim-release-peer"

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))

    f.write_text("v2\n", encoding="utf-8")
    _own_sid(monkeypatch, sid)
    session_core.init(sid, cwd=str(repo))

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    peer_result = claim_index.lookup([rel], cwd=str(repo))
    assert peer_sid in peer_result.get(rel, [])


def test_unresolvable_sid_commit_lands_claim_retained(repo, monkeypatch):
    """(c) with the sid unresolvable, the commit still lands and the claim
    (a peer's, since this call has no sid of its own to have claimed under)
    is RETAINED -- release is skipped explicitly, never guessed."""
    peer_sid = "commit-v2-claim-release-peer-2"

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)

    session_core.init(peer_sid, cwd=str(repo))
    session_scope.touch(peer_sid, rel, cwd=str(repo))

    f.write_text("v2\n", encoding="utf-8")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    peer_result = claim_index.lookup([rel], cwd=str(repo))
    assert peer_sid in peer_result.get(rel, [])


def test_raising_release_does_not_turn_landed_commit_into_failure(repo, monkeypatch):
    sid = "commit-v2-claim-release-raise"
    _own_sid(monkeypatch, sid)

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)
    f.write_text("v2\n", encoding="utf-8")

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(commit_v2.session_scope, "release_committed_claims", _boom)

    result = _call(repo, {"paths": [rel], "message": "update tracker"})

    assert result["committed"] is True
    assert result["sha"] is not None

    result2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    )
    assert result["sha"] == result2.stdout.strip()
