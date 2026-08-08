"""
coordinator_core.session.tests.test_worktree_safety — tests for
coordinator_core.session.worktree_safety.history_rewrite_verdict.

Fixture idiom matches test_liveness.py: a real tmp_path git repo with
``.git/coordinator-sessions/<sid>/meta.json`` fixtures, liveness driven through the
real ``coordinator_core.session.liveness`` module (no mocking of liveness itself —
only ``live_session_ids`` is monkeypatched where a test needs to force the FAIL-CLOSED
exception path, since that path cannot be reached via any real on-disk fixture).

Spec backlink: workday-complete-ceremony-hardening spinoff, oracle finding F4.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, liveness, worktree_safety


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _live_meta():
    return {"pid": "1", "last_activity": core.now_iso()}


class TestHistoryRewriteVerdictOk:
    def test_ok_when_only_live_session_is_self(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"
        assert v.peer_session_ids == ()

    def test_ok_when_no_sessions_at_all(self, tmp_path):
        repo = _make_repo(tmp_path)
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"

    def test_ok_when_no_live_sessions_and_no_self_identity(self, tmp_path):
        repo = _make_repo(tmp_path)
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="")
        assert v.outcome == "ok"
        assert v.peer_session_ids == ()


class TestHistoryRewriteVerdictRefused:
    def test_refused_when_live_peer_exists(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-sid", _live_meta())
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "refused"
        assert v.peer_session_ids == ("peer-sid",)
        assert "peer-sid" in v.reason
        assert "1" in v.reason

    def test_refused_names_multiple_peers(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-b", _live_meta())
        _write_session(repo, "peer-a", _live_meta())
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "refused"
        assert v.peer_session_ids == ("peer-a", "peer-b")
        assert "peer-a" in v.reason and "peer-b" in v.reason
        assert "2" in v.reason

    def test_stale_peer_not_counted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(
            repo, "stale-peer", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"


class TestHistoryRewriteVerdictUnknownFailClosed:
    def test_unknown_when_liveness_probe_raises(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        def _boom(cwd=None):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(worktree_safety._liveness, "live_session_ids", _boom)
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "unknown"
        assert v.outcome != "ok"

    def test_unknown_when_self_unresolvable_and_live_set_nonempty(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _write_session(repo, "peer-sid", _live_meta())
        # No env var and no sentinel file in this fresh repo -> self is unresolvable.
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="")
        assert v.outcome == "unknown"
        assert v.outcome != "ok"
        assert v.peer_session_ids == ("peer-sid",)

    def test_self_session_id_override_takes_priority_over_env_and_sentinel(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sid")
        _write_session(repo, "env-sid", _live_meta())
        # override says we ARE "env-sid" too — should still resolve to ok (self-only).
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="env-sid")
        assert v.outcome == "ok"

    def test_env_var_resolution_used_when_no_override(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-sid", _live_meta())
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo))
        assert v.outcome == "refused"
        assert v.peer_session_ids == ("peer-sid",)

    def test_sentinel_file_ignored_KS3(self, tmp_path, monkeypatch):
        """KS-3 (2026-08-07): the `.current-session-id` sentinel tier was
        removed from resolve_self_session_id. A well-formed sentinel file
        must NOT be consulted — with no env var, self-identity is
        unresolvable, which (live set non-empty) degrades to "unknown"
        (fail closed), never "ok" and never a refuse->allow flip."""
        repo = _make_repo(tmp_path)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        sentinel_dir = Path(repo) / ".git" / "coordinator-sessions"
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        (sentinel_dir / ".current-session-id").write_text("self-sid\n")
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-sid", _live_meta())
        assert worktree_safety.resolve_self_session_id(str(repo)) == ""
        v = worktree_safety.history_rewrite_verdict(cwd=str(repo))
        assert v.outcome == "unknown"
        assert v.outcome != "ok"


def test_outcome_is_always_one_of_the_three_values(tmp_path):
    repo = _make_repo(tmp_path)
    v = worktree_safety.history_rewrite_verdict(cwd=str(repo), self_session_id="x")
    assert v.outcome in ("ok", "refused", "unknown")


class TestBranchMutationVerdict:
    def test_ok_when_only_live_session_is_self(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"
        assert v.peers == ()

    def test_ok_when_no_sessions_at_all(self, tmp_path):
        repo = _make_repo(tmp_path)
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"

    def test_refused_names_peer_and_its_branch(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-sid", {**_live_meta(), "branch": "work/peer/2026-01-01"})
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "refused"
        assert v.peers == (("peer-sid", "work/peer/2026-01-01"),)
        assert "peer-sid" in v.reason
        assert "work/peer/2026-01-01" in v.reason

    def test_refused_peer_with_no_branch_field(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(repo, "peer-sid", _live_meta())
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "refused"
        assert v.peers == (("peer-sid", None),)

    def test_stale_peer_not_counted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(repo, "self-sid", _live_meta())
        _write_session(
            repo, "stale-peer", {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}
        )
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "ok"

    def test_unknown_when_liveness_probe_raises(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)

        def _boom(cwd=None):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(worktree_safety._liveness, "live_session_verdicts", _boom)
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="self-sid")
        assert v.outcome == "unknown"

    def test_unknown_when_self_unresolvable_and_live_set_nonempty(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _write_session(repo, "peer-sid", _live_meta())
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="")
        assert v.outcome == "unknown"
        assert v.peers == (("peer-sid", None),)

    def test_outcome_is_always_one_of_the_three_values(self, tmp_path):
        repo = _make_repo(tmp_path)
        v = worktree_safety.branch_mutation_verdict(cwd=str(repo), self_session_id="x")
        assert v.outcome in ("ok", "refused", "unknown")
