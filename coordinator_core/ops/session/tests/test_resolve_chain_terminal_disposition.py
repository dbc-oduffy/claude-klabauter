"""
coordinator_core.ops.session.tests.test_resolve_chain_terminal_disposition

Coverage added under KS-5 (2026-08-07): the epoch-tail fabricated-id
fallback (`_resolve_session_id`'s former P6 tier) was removed as strictly
worse than the `.current-session-id` sentinel KS-3 removed just before it —
a fabricated id is different on every invocation and indistinguishable to a
downstream reader from a real one, so it silently promoted the
"nothing resolved" case into a passing "open"/`chain_terminal: False`
verdict that SKIPS the workstream-complete chain-end coverage gate.

This module was reported missing from the shared worktree by a concurrent
session on 2026-08-07 (see the KS-5 chunk brief); it did not exist before
this commit. It exercises ONLY the unresolved-sid guard this chunk adds —
`resolve_chain_terminal_disposition.py`'s dual-detector classification logic
already has broader native-rewrite coverage elsewhere in the corpus (see
that module's own spec backlinks); duplicating it here is out of scope.

Spec backlink: coordinator_core/ops/session/resolve_chain_terminal_disposition.py

Negative-spec:
  - Does NOT exercise the dual-detector (live-claim / archive / git-provenance)
    classification paths — those require a real claimed/archived handoff
    fixture, out of scope for this chunk's unresolved-sid guard.
  - Does NOT assert anything about the wsc-session-disposition.py CLI sibling
    (coordinator/bin/tests/test_wsc_session_disposition.py covers that one).
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

import coordinator_core.ops.session.resolve_chain_terminal_disposition as rctd

# _make_repo spawns real git per test (init/config/add/commit); declared to
# the spawn ratchet rather than grandfathered in its frozen baseline --
# see coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


class TestResolveSessionIdUnresolvedTier:
    def test_no_tier_resolves_returns_empty_never_fabricated(self):
        sid, source = rctd._resolve_session_id(None, None, {})
        assert sid == ""
        assert source == "unresolved"

    def test_no_tier_resolves_regression_guard_against_epoch_shape(self):
        """Direct perturbation guard: a reverted fix would return a 6-digit
        epoch-tail string here, which — being non-empty — would slip past
        every `if not sid` guard downstream undetected."""
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

        # The load-bearing property: this must NOT read as a passing gate.
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
        """Regression guard for the specific false-match hazard an empty sid
        introduces: `_claim_holder` also returns "" for a genuinely unclaimed
        handoff, so a naive `"" == sid` scan would read every unclaimed
        record as "claimed by this session" if the unresolved-sid guard were
        removed. Seed exactly that shape and prove the guard still fires
        before any scan runs."""
        repo = _make_repo(tmp_path)
        handoffs_dir = repo / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        (handoffs_dir / "unclaimed.md").write_text(
            "---\npredecessor: none\n---\nbody\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add unclaimed handoff"], cwd=repo, check=True)

        result = rctd._classify_sync(repo, None, {})
        assert result["exit_code"] != 0
        assert result["chain_terminal"] is False
        assert result["disposition"] is None

    def test_resolved_sid_path_unaffected_by_the_guard(self, tmp_path):
        """Sanity: a normal resolved sid with nothing claimed still resolves
        the pre-existing open/single-session verdict via exit_code 0 — the
        guard only fires on a genuinely unresolved sid."""
        repo = _make_repo(tmp_path)
        result = rctd._classify_sync(repo, "real-session-id", {})
        assert result["exit_code"] == 0
        assert result["disposition"] == "open"
        assert result["chain_terminal"] is False
        assert result["evidence"]["session_id"] == "real-session-id"
        assert result["evidence"]["session_id_source"] == "param"


class TestDetectorBPositiveOwnership:
    """2026-08-10 archive-leg touch-vs-consume incident (example-retrieval-repo memo
    `2026-08-10-example-retrieval-repo-em-wsc-archive-leg-infers-consumption-from-a-
    touch.md`). Mirrors the fix landed in this module's `bin` sibling
    (`coordinator/bin/wsc-session-disposition.py::_foreign_consumer_guard`,
    covered by `coordinator/bin/tests/test_wsc_session_disposition.py`) — the
    two copies are independently maintained, so the regression needs a test on
    each side, not one."""

    @staticmethod
    def _repo_with_archived_touch(tmp_path, sid, frontmatter, subject):
        repo = _make_repo(tmp_path)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True
        )
        archive_dir = repo / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        (archive_dir / "2026-08-10_144028_peer-baton.md").write_text(
            frontmatter, encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"{subject}\n\nSession-Id: {sid}"],
            cwd=repo,
            check=True,
        )
        # `_classify_sync`'s first arg is the git COMMON DIR, not the worktree
        # root (_OP_KEY_SCOPE = "common_dir"); it derives the worktree via
        # `main_worktree_root`, which takes the parent. Handing it the worktree
        # root instead silently classifies the parent directory — Detector B
        # then fails its merge-base and is skipped, which reads as a clean
        # "open" verdict rather than an error.
        return repo / ".git"

    def test_ownerless_looking_record_is_not_read_as_consumed(self, tmp_path):
        """The observed shape: a live peer's baton whose ledger claim is
        liveness-gated away and whose mirror carries `status: claimed` with no
        `claimed_by:`, re-added by an ordinary-prose restore commit. Both
        negative-evidence reads come back empty — which must now REJECT, not
        fall through to acceptance."""
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
        """Regression guard: positively-evidenced ownership must still resolve
        chain-terminal — the tightening rejects only the no-evidence case.
        Resolves via Detector A (the archived claim stamp), which is exactly
        the point: an own-claim record never needs the git-provenance leg, so
        tightening that leg cannot cost the legitimate path."""
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
