
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.detached_render_commit import commit_own_artifact
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags())


def _own_sid(monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim_cleared(repo: Path, sid: str, rel_path: str) -> bool:
    result = claim_index.lookup([rel_path], cwd=str(repo))
    return sid not in result.get(rel_path, [])


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


def test_commit_own_artifact_releases_claim_on_landed_commit(repo, monkeypatch):
    sid = "detached-render-commit-claim-test"
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

    ok = commit_own_artifact(repo, rel, "test: update tracker", caller_label="test")

    assert ok is True
    assert _claim_cleared(repo, sid, rel)


def test_commit_own_artifact_noop_when_clean_does_not_release(repo, monkeypatch):
    sid = "detached-render-commit-noop-test"
    _own_sid(monkeypatch, sid)

    rel = "state/handoff-tracker.md"
    f = repo / rel
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", "seed tracker"], repo)

    session_core.init(sid, cwd=str(repo))
    session_scope.touch(sid, rel, cwd=str(repo))

    ok = commit_own_artifact(repo, rel, "test: no-op", caller_label="test")

    assert ok is True
    result = claim_index.lookup([rel], cwd=str(repo))
    assert sid in result.get(rel, [])
