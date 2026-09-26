
from __future__ import annotations

import asyncio
import subprocess

import pytest

import coordinator_core.ops.session.resolve_chain_terminal_disposition as rctd
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


class TestResolveSessionIdUnresolvedTier:
    def test_no_tier_resolves_returns_empty_never_fabricated(self):
        sid, source = rctd._resolve_session_id(None, None, {})
        assert sid == ""
        assert source == "unresolved"

    def test_no_tier_resolves_regression_guard_against_epoch_shape(self):
        sid, _ = rctd._resolve_session_id(None, None, {})
        assert not (len(sid) == 6 and sid.isdigit())

    def test_param_sid_still_wins(self):
        sid, source = rctd._resolve_session_id("explicit", None, {})
        assert sid == "explicit"
        assert source == "param"

    def test_env_tier_still_resolves(self):
        sid, source = rctd._resolve_session_id(None, None, {"em_sid": "env-sid"})
        assert sid == "env-sid"
        assert source == "em_sid"


class TestClassifySyncUnresolvedGuard:
    def test_unresolved_sid_yields_error_never_a_clean_open_verdict(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = rctd._classify_sync(repo, None, {})

        assert result["exit_code"] != 0
        assert result["disposition"] is None
        assert result["chain_terminal"] is False

    def test_unresolved_sid_evidence_labelled_unresolved_not_silently_open(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = rctd._classify_sync(repo, None, {})

        evidence = result["evidence"]
        assert evidence["session_id"] == ""
        assert evidence["session_id_source"] == "unresolved"

    def test_unresolved_sid_never_spuriously_matches_an_unclaimed_handoff(self, tmp_path):
        repo = _make_repo(tmp_path)
        handoffs_dir = repo / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "unclaimed.md").write_text(
            "---\npredecessor: none\n---\nbody\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(["git", "commit", "-q", "-m", "add unclaimed handoff"], cwd=repo, check=True, **no_console_passthrough_kwargs())

        result = rctd._classify_sync(repo, None, {})
        assert result["exit_code"] != 0
        assert result["chain_terminal"] is False
        assert result["disposition"] is None

    def test_resolved_sid_path_unaffected_by_the_guard(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = rctd._classify_sync(repo, "real-session-id", {})
        assert result["exit_code"] == 0
        assert result["disposition"] == "open"
        assert result["chain_terminal"] is False
        assert result["evidence"]["session_id"] == "real-session-id"
        assert result["evidence"]["session_id_source"] == "param"


class TestDetectorBPositiveOwnership:

    @staticmethod
    def _repo_with_archived_touch(tmp_path, sid, frontmatter, subject):
        repo = _make_repo(tmp_path)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True,
            **no_console_passthrough_kwargs(),
        )
        archive_dir = repo / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        (archive_dir / "2026-08-10_144028_peer-baton.md").write_text(
            frontmatter, encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "-m", f"{subject}\n\nSession-Id: {sid}"],
            cwd=repo,
            check=True,
            **no_console_passthrough_kwargs(),
        )
        # root (_OP_KEY_SCOPE = "common_dir"); it derives the worktree via
        return repo / ".git"

    def test_ownerless_looking_record_is_not_read_as_consumed(self, tmp_path):
        sid = "ddadea9e-0000-0000-0000-000000000000"
        repo = self._repo_with_archived_touch(
            tmp_path,
            sid,
            "---\nstatus: claimed\npredecessor: state/handoffs/2026-08-10-peer.md\n"
            "deployment_state: continued\n"
            "authoring_session: 9c0c419d-def6-4b98-90ba-42d2580e870a\n---\nbody\n",
            "restore: re-track a peer's archived handoff my amend swept out",
        )

        result = rctd._classify_sync(repo, sid, {})
        assert result["exit_code"] == 0
        assert result["disposition"] == "open"
        assert result["chain_terminal"] is False
        assert result["evidence"]["consumed_handoff"] is None
        assert any(
            "is not evidence of consuming it" in note
            for note in result["evidence"]["notes"]
        ), result["evidence"]["notes"]

    def test_own_claim_still_resolves_chain_terminal(self, tmp_path):
        sid = "ddadea9e-0000-0000-0000-000000000000"
        repo = self._repo_with_archived_touch(
            tmp_path,
            sid,
            f"---\nclaimed_by: {sid}\npredecessor: none\n"
            "deployment_state: continued\n---\nbody\n",
            "ship and archive my own predecessor",
        )

        result = rctd._classify_sync(repo, sid, {})
        assert result["exit_code"] == 0
        assert result["chain_terminal"] is True
        assert result["disposition"] == "continued"


class TestDetectorAMultipleClaimedArchivedHandoffs:

    def test_last_sorted_archived_claim_wins_not_the_first(self, tmp_path):
        sid = "chain-sid-multi-claim"
        repo = _make_repo(tmp_path)
        archive_dir = repo / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        (archive_dir / "2026-01-01-earlier.md").write_text(
            f"---\nclaimed_by: {sid}\npredecessor: none\n"
            "deployment_state: closed\n---\nbody\n",
            encoding="utf-8",
        )
        (archive_dir / "2026-06-01-later.md").write_text(
            f"---\nclaimed_by: {sid}\npredecessor: none\n"
            "deployment_state: continued\n---\nbody\n",
            encoding="utf-8",
        )

        result = rctd._classify_sync(repo, sid, {})

        assert result["exit_code"] == 0
        assert result["disposition"] == "continued"
        assert result["chain_terminal"] is True
        assert (
            result["evidence"]["consumed_handoff"]
            == "archive/handoffs/2026-06-01-later.md"
        )


class TestHandlerUnresolvedGuard:
    def test_handler_returns_error_envelope_when_sid_unresolvable(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        for var in ("em_sid", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        result = asyncio.run(rctd._handler({}, repo_root=repo))
        assert result["exit_code"] == 1
        assert result["disposition"] is None
        assert result["chain_terminal"] is False
        assert "error" in result
        assert result["evidence"]["session_id_source"] == "unresolved"
