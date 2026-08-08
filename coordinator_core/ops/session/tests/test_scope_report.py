"""
coordinator_core.ops.session.tests.test_scope_report

Tests for coordinator_core.ops.session.scope_report — the C4a chunk of
docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md:
(a) the registered read-only op ``session.scope_report``, and (b) the
in-process ownership helper ``assert_paths_in_session_scope`` that C4b
(guard) and C4c (sink) both import directly.

Import guard: coordinator_core.ops.session.scope_report MUST be imported at
module load time to fire the @register_op("session.scope_report")
side-effect and populate _REGISTRY (lesson: state/lessons/2026-07-04-
universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) assert_paths_in_session_scope — own-session paths allow; another live
      session's paths deny; an unowned/orphan path denies; empty paths deny;
      unresolvable/empty session_id denies; a compute_offer error (monkey-
      patched to raise) denies; a mixed set (one owned + one not) denies
      whole (allow-list, not per-path partial allow).
  (b) session.scope_report op — returns compute_offer's own shape verbatim
      for an explicit session_id; resolves the calling session via
      core.resolve_session_id when session_id is omitted; an unresolvable
      session_id returns an error envelope rather than raising; the op
      makes no git/touched.txt mutation (dirty set unchanged before/after).

Spec backlink: coordinator_core/ops/session/scope_report.py

Negative-spec:
  - Does NOT exercise compute_offer's own scope-math edge cases in depth —
    those already have dedicated coverage in test_safe_commit_offer.py;
    this file only proves scope_report's OWN composition/allow-list/
    fail-closed contract on top of it.
  - Does NOT assert anything about block_subagent_commit or
    scoped_git_commit — those are C4b/C4c, out of scope for this chunk.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.session.scope_report  # noqa: F401 — fires @register_op("session.scope_report")

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session import safe_commit_offer, scope_report
from coordinator_core.ops.session.scope_report import (
    _handler,
    assert_paths_in_session_scope,
)
from coordinator_core.session import core, scope

_OP_NAME = "session.scope_report"
_session_registered_ops = [k for k in _REGISTRY if k.startswith("session.")]
assert len(_session_registered_ops) >= 1, (
    "import guard failed: no session.* ops in _REGISTRY — "
    "check coordinator_core.ops.session.scope_report import and @register_op side-effect"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.scope_report @register_op('session.scope_report') did not fire"
)


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _dirty_status(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


# ---------------------------------------------------------------------------
# (a) assert_paths_in_session_scope
# ---------------------------------------------------------------------------


class TestAssertPathsInSessionScope:
    def test_own_session_paths_allow(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        ok, reason = assert_paths_in_session_scope("mine", ["a.py"], cwd=str(repo))
        assert ok is True
        assert reason == ""

    def test_another_live_sessions_paths_deny(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))

        # Mutation-check baseline: compute_offer itself excludes shared.py
        # from "mine"'s safe_paths (peer claim wins) — assert the helper's
        # denial tracks that, not an independent re-derivation.
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "shared.py" not in offer["safe_paths"]

        ok, reason = assert_paths_in_session_scope("mine", ["shared.py"], cwd=str(repo))
        assert ok is False
        assert reason

    def test_unowned_orphan_path_denies(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        # A dirty file nobody's touched.txt ever recorded, and predating the
        # session's own started_at — compute_scope's Step-2 mtime fallback
        # (narrowed per DR-246) withholds it from safe_paths entirely.
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok is False
        assert reason

    def test_empty_paths_denies(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        ok, reason = assert_paths_in_session_scope("mine", [], cwd=str(repo))
        assert ok is False
        assert reason

    def test_unresolvable_session_id_denies(self, tmp_path):
        repo = _make_repo(tmp_path)
        ok, reason = assert_paths_in_session_scope("", ["a.py"], cwd=str(repo))
        assert ok is False
        assert reason

        ok2, reason2 = assert_paths_in_session_scope(None, ["a.py"], cwd=str(repo))  # type: ignore[arg-type]
        assert ok2 is False
        assert reason2

    def test_compute_offer_error_denies(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        def _boom(session_id, cwd=None):
            raise RuntimeError("simulated compute_offer failure")

        monkeypatch.setattr(scope_report, "compute_offer", _boom)

        ok, reason = assert_paths_in_session_scope("mine", ["a.py"], cwd=str(repo))
        assert ok is False
        assert "simulated compute_offer failure" in reason

    def test_partial_ownership_denies_whole_set(self, tmp_path):
        # Allow-list polarity: ONE path outside scope denies the whole
        # pathspec, even when another element of the same call IS owned.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope("mine", ["a.py", "orphan.py"], cwd=str(repo))
        assert ok is False
        assert reason

    def test_mutation_check_reverting_fix_would_pass_unowned_path(self, tmp_path):
        """Mutation-check: a denylist-shaped ("allow unless owned by a
        named-live peer") reimplementation would let an UNOWNED orphan path
        through — the exact harm case this helper exists to prevent. Prove
        the real helper denies it (already covered above); this test pins
        the CONTRAST directly against a deliberately-wrong denylist
        reimplementation, so a future edit that silently reverts the
        allow-list polarity is caught here even if the orphan-specific test
        above is weakened."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "orphan.py").write_text("o")  # nobody's claim

        def _wrong_denylist_shaped(session_id, paths, cwd=None):
            offer = safe_commit_offer.compute_offer(session_id, cwd)
            owned_by_peer = {
                e["path"] for e in offer["excluded"] if e["reason"].startswith("owned by session")
            }
            for p in paths:
                if p in owned_by_peer:
                    return False, "owned by a peer"
            return True, ""

        # The deliberately-wrong shape wrongly allows the unowned orphan.
        assert _wrong_denylist_shaped("mine", ["orphan.py"], cwd=str(repo)) == (True, "")

        # The REAL helper denies it.
        ok, reason = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok is False
        assert reason

    def test_multi_path_denial_enumerates_every_denied_path(self, tmp_path):
        """SC-DR-019: a scoped-commit refusal is per-path, not per-commit —
        the deny reason must name EVERY denied path, not just the first, so
        an operator does not have to re-derive the split by hand."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope(
            "mine", ["shared.py", "orphan.py"], cwd=str(repo)
        )
        assert ok is False
        assert "'shared.py'" in reason
        assert "'orphan.py'" in reason
        assert "orphan" in reason
        # CORRECTED 2026-08-07 (pass 2): this assertion used to read "claimed
        # by DEAD session other (claim is reapable)", and it PASSED — but it
        # was pinning the defect, not the property. "other" is init'd IN THIS
        # FIXTURE REPO with a fresh `last_activity`, so it is genuinely live;
        # it only read DEAD because the zero-arg oracle resolved its session
        # registry from the PROCESS cwd (the real claude-klabauter checkout) instead of
        # `cwd` (this tmp repo), where "other" does not exist at all. That is
        # the same cross-repo miss the example-doctrine-repo incident hit against a real
        # peer. With the oracle cwd-scoped, the end-to-end path now earns the
        # right verdict against the real oracle — which is what this
        # assertion was always meant to prove.
        assert "claimed by live session other" in reason
        assert "reapable" not in reason

    def test_allowed_remainder_named_as_committable_and_exact(self, tmp_path):
        """The uncontested remainder (allowed paths) appears in the message
        and is EXACTLY the subset that was not denied."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope(
            "mine", ["a.py", "orphan.py"], cwd=str(repo)
        )
        assert ok is False
        assert "'orphan.py'" in reason
        assert "committable" in reason
        assert "'a.py'" in reason
        # The denied path must not appear as part of the committable list.
        committable_section = reason.split("committable now")[1]
        assert "orphan.py" not in committable_section

    def test_stable_prefix_still_matches_single_path_denial(self, tmp_path):
        """Regression guard for existing pinned callers: the message still
        STARTS WITH the stable prefix for the first (only) denied path."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok is False
        assert reason.startswith(
            "path outside session mine scope: 'orphan.py' "
            "(orphan — dirty but claimed by no session)"
        )

    def test_empty_remainder_reads_sensibly_when_all_paths_denied(self, tmp_path):
        """All paths in the pathspec denied — no committable remainder; the
        message says so explicitly rather than emitting an empty list."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope(
            "mine", ["shared.py", "orphan.py"], cwd=str(repo)
        )
        assert ok is False
        assert "no committable remainder" in reason


class TestAssertPathsInSessionScopeAllowOrphans:
    """Orphan-aware ownership gate (sizing:
    2026-08-03-ownership-gate-cannot-distinguish-an-orp.yaml) — the
    `allow_orphans` kwarg."""

    def _seed_orphan(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        # Old started_at (mirrors test_unowned_orphan_path_denies above) so
        # the Step-2 mtime fallback deterministically picks the file up as a
        # candidate regardless of clock precision, without giving it a real
        # touched.txt claim — the file still lands in orphans, not my_scope.
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")
        # No touched.txt record anywhere, and no other session dir either —
        # compute_scope's Step 5 classifies this as a genuine orphan (dirty,
        # claimed by no session at all), not merely mtime-excluded.
        return repo

    def test_orphan_default_mode_denies_and_names_orphan(self, tmp_path):
        repo = self._seed_orphan(tmp_path)
        ok, reason = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok is False
        assert "orphan" in reason
        assert "peer" not in reason and "live session" not in reason

    def test_orphan_allow_orphans_true_adopts_now_that_r1_is_closed(self, tmp_path):
        """Inverted per this test's own prior docstring: staff-eng R1
        (2026-08-03) is now closed (`ScopeResult.indeterminate` +
        `compute_offer`'s whole-call orphan withhold — see
        `scope._ORPHAN_ADOPTION_ENABLED`'s current negative spec) and
        `_ORPHAN_ADOPTION_ENABLED` is flipped to `True`. A genuine orphan, a
        real initialized session (`_seed_orphan` calls `core.init`, so
        `_session_has_positive_evidence` is satisfied), `allow_orphans=True`
        — now ALLOWED.
        """
        repo = self._seed_orphan(tmp_path)
        ok, reason = assert_paths_in_session_scope(
            "mine", ["orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok is True
        assert reason == ""

    def test_peer_claimed_path_denies_in_both_modes(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))

        ok_default, reason_default = assert_paths_in_session_scope(
            "mine", ["shared.py"], cwd=str(repo)
        )
        assert ok_default is False
        assert "other" in reason_default

        ok_allow, reason_allow = assert_paths_in_session_scope(
            "mine", ["shared.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok_allow is False
        assert "other" in reason_allow

    def test_mixed_pathspec_allow_orphans_true_still_denies_on_peer_claim(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        ok, reason = assert_paths_in_session_scope(
            "mine", ["shared.py", "orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok is False
        assert reason

    def test_degraded_read_denies_in_both_modes(self, tmp_path, monkeypatch):
        repo = self._seed_orphan(tmp_path)

        def _boom(session_id, cwd=None):
            raise RuntimeError("simulated compute_offer failure")

        monkeypatch.setattr(scope_report, "compute_offer", _boom)

        ok_default, _ = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok_default is False
        ok_allow, _ = assert_paths_in_session_scope(
            "mine", ["orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok_allow is False

    def test_missing_orphans_list_denies_in_both_modes(self, tmp_path, monkeypatch):
        repo = self._seed_orphan(tmp_path)
        real_compute_offer = safe_commit_offer.compute_offer

        def _no_orphans_key(session_id, cwd=None):
            offer = dict(real_compute_offer(session_id, cwd))
            offer.pop("orphans", None)
            return offer

        monkeypatch.setattr(scope_report, "compute_offer", _no_orphans_key)

        ok_default, _ = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok_default is False
        ok_allow, _ = assert_paths_in_session_scope(
            "mine", ["orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok_allow is False

    def test_r1_non_candidate_path_with_unreadable_peer_claim_still_denies(
        self, tmp_path
    ):
        """Staff-eng R1 regression, the EXACT non-candidate shape (pass-2
        re-review probe A, not probe B): a dirty path that this call never
        adopted as a candidate at all (its mtime predates `started_at`, so
        `compute_scope` Step 2 never adds it to `touched_set`), while a
        DIFFERENT peer session's claim set is unreadable this call. Before
        the `ScopeResult.indeterminate` fix, this path had no `skipped`
        counterpart (Step 4 never saw it as a candidate to withhold) and
        reached `compute_offer`'s `orphans` verbatim -- `allow_orphans=True`
        admitted it even though a live peer's claim set was degraded this
        call. This is the shape probe B (a candidate this session itself
        touched) does NOT cover -- see `test_peer_claimed_path_denies_in_
        both_modes` above for that one.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        # Push started_at into the future so peer.md's mtime (set below,
        # AFTER this call) never satisfies compute_scope Step 2's
        # `mtime >= started_at_epoch` test -- peer.md never becomes a
        # candidate for "mine" at all.
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        from datetime import datetime, timezone

        future = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
        )
        (sdir / "started_at").write_text(
            future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
        )

        core.init("peer", cwd=str(repo))
        peer_touched = Path(core.session_dir("peer", cwd=str(repo))) / "touched.txt"
        peer_touched.write_text("peer.md\n", encoding="utf-8")
        (repo / "peer.md").write_text("peer's own in-flight work")
        os.chmod(str(peer_touched), 0o000)
        try:
            ok, reason = assert_paths_in_session_scope(
                "mine", ["peer.md"], cwd=str(repo), allow_orphans=True
            )
        finally:
            os.chmod(str(peer_touched), 0o644)

        assert ok is False
        assert reason

    def test_compute_offer_existing_keys_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert set(offer["safe_paths"]) == {"a.py"}
        assert {"path": "shared.py", "reason": "owned by session other"} in offer["excluded"]
        assert "orphans" in offer
        assert isinstance(offer["orphans"], list)


class TestAlreadyCleanClassification:
    """`scoped_git_commit.py`'s Half 2 fix (mixed dirty+clean pathspec
    incident): a path that is already clean at HEAD is a NOTHING-TO-COMMIT
    condition, not an ownership question -- `_classify_denied_path` must name
    that distinctly from `_CLASSIFICATION_UNCLASSIFIED` ("unclaimed/never
    classified by compute_offer"), which is an ownership claim the caller
    would (wrongly) act on via `include_orphans: true`. See
    `coordinator_core.ops.ceremony.scoped_git_commit`'s Half 1/Half 2 fix.
    """

    def test_already_clean_path_gets_its_own_classification(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        # README.md is clean at HEAD (committed by _make_repo, untouched
        # since) -- never dirty, never a member of safe_paths/orphans.
        ok, reason = assert_paths_in_session_scope(
            "mine", ["README.md"], cwd=str(repo), already_clean=["README.md"],
        )

        assert ok is False
        assert "already clean at HEAD" in reason
        assert "nothing to commit" in reason
        # Must NOT read as an ownership/adoption problem.
        assert "include_orphans" not in reason
        assert "unclaimed/never classified" not in reason

    def test_classify_denied_path_already_clean_directly(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        classification = scope_report._classify_denied_path(
            "README.md", offer, offer.get("orphans") or [], already_clean=True,
        )

        assert "already clean at HEAD" in classification
        assert "include_orphans" not in classification
        assert classification != scope_report._CLASSIFICATION_UNCLASSIFIED


class TestClassifyDeniedPathLiveness:
    """`_classify_denied_path`'s ownership branch must EARN "live", never
    assert it from a bare format-string interpolation of `compute_offer`'s
    reason field (break-class fix, 2026-08-07 — example-doctrine-repo report). Three
    branches, exercised directly against a minimal offer dict so each is
    deterministic — liveness monkeypatched at the `scope_report` module
    seam rather than relying on a real session's actual live/dead state.
    `test_multi_path_denial_enumerates_every_denied_path` above proves the
    same contract end-to-end against the real oracle."""

    def _offer_with_reason(self, reason):
        return {"excluded": [{"path": "shared.py", "reason": reason}]}

    def test_bare_sid_live_earns_live_wording(self, monkeypatch):
        offer = self._offer_with_reason("owned by session other")
        monkeypatch.setattr(
            scope_report, "live_session_ids", lambda cwd=None: frozenset({"other"})
        )
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == "claimed by live session other"

    def test_bare_sid_not_live_names_the_read_but_withholds_the_remedy(self, monkeypatch):
        """A refusal must never prescribe reaping (2026-08-07 memo, break-class):
        the remedy is the actionable half of the message, and acting on it means
        committing over a peer's in-flight surface. Name the observation, not a
        licence."""
        offer = self._offer_with_reason("owned by session other")
        monkeypatch.setattr(
            scope_report, "live_session_ids", lambda cwd=None: frozenset({"someone-else"})
        )
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == (
            "claimed by session other (holder reads NOT live in this repo; "
            "NOT a licence to reap — re-verify the holder before any takeover)"
        )
        assert "reapable" not in classification

    def test_liveness_oracle_is_asked_about_the_target_repo_not_the_process_cwd(
        self, monkeypatch
    ):
        """Session registries are per-repo (`<repo>/.git/coordinator-sessions/`),
        so "is <sid> live?" is only answerable relative to a repo. The zero-arg
        oracle resolved from the PROCESS cwd, so a cross-repo invocation read
        every peer of the TARGET repo as not-live — the reported incident.
        `cwd` must reach the oracle."""
        offer = self._offer_with_reason("owned by session other")
        seen = []

        def _oracle(cwd=None):
            seen.append(cwd)
            return frozenset({"other"}) if cwd == "/target/repo" else frozenset({"noise"})

        monkeypatch.setattr(scope_report, "live_session_ids", _oracle)
        classification = scope_report._classify_denied_path(
            "shared.py", offer, [], cwd="/target/repo"
        )
        assert seen == ["/target/repo"]
        assert classification == "claimed by live session other"

    def test_assert_paths_threads_cwd_through_to_the_oracle(self, monkeypatch):
        """End-to-end on the threading itself: the `cwd` the caller passes to
        `assert_paths_in_session_scope` (already used for `compute_offer` and
        the positive-evidence check) must be the same one the liveness oracle
        answers for — they must not disagree about WHICH repo."""
        monkeypatch.setattr(
            scope_report,
            "compute_offer",
            lambda session_id, cwd: {
                "safe_paths": [],
                "excluded": [{"path": "shared.py", "reason": "owned by session other"}],
                "orphans": [],
            },
        )
        seen = []
        monkeypatch.setattr(
            scope_report,
            "live_session_ids",
            lambda cwd=None: (seen.append(cwd) or frozenset({"other"})),
        )
        ok, reason = scope_report.assert_paths_in_session_scope(
            "me", ["shared.py"], "/target/repo"
        )
        assert ok is False
        assert seen == ["/target/repo"]
        assert "claimed by live session other" in reason

    def test_non_bare_token_remainder_never_asserts_dead(self, monkeypatch):
        """`safe_commit_offer`'s degraded-owner reason ("unknown (claims
        unreadable: other)") is not a session id — feeding it to the oracle
        would deterministically read as not-live and mint a NEW false DEAD
        assertion, exactly the defect this fix removes. The oracle must not
        even be consulted for this shape."""
        offer = self._offer_with_reason("owned by session unknown (claims unreadable: other)")
        calls = []
        monkeypatch.setattr(
            scope_report,
            "live_session_ids",
            lambda cwd=None: (calls.append(1) or frozenset()),
        )
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == (
            "claimed by session unknown (claims unreadable: other) (liveness not checked)"
        )
        assert not calls  # oracle never consulted for a non-bare-token remainder

    def test_agent_race_window_remainder_also_routes_to_branch_three(self, monkeypatch):
        offer = self._offer_with_reason(
            "owned by session unknown (agent race window, no em-session-id.txt yet)"
        )

        def _must_not_be_called(cwd=None):
            raise AssertionError("oracle must not be called for a non-bare-token remainder")

        monkeypatch.setattr(scope_report, "live_session_ids", _must_not_be_called)
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == (
            "claimed by session unknown (agent race window, no em-session-id.txt yet) "
            "(liveness not checked)"
        )

    def test_oracle_raising_is_treated_as_unknown_not_dead(self, monkeypatch):
        """The oracle's documented empty-on-error contract makes an empty
        result indistinguishable from "nothing is live" — an oracle exception
        must not be allowed to masquerade as a confirmed-dead verdict either."""
        offer = self._offer_with_reason("owned by session other")

        def _boom(cwd=None):
            raise RuntimeError("simulated liveness oracle failure")

        monkeypatch.setattr(scope_report, "live_session_ids", _boom)
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == "claimed by session other (liveness not checked)"
        assert "DEAD" not in classification

    def test_oracle_empty_result_treated_as_unknown_not_dead(self, monkeypatch):
        offer = self._offer_with_reason("owned by session other")
        monkeypatch.setattr(
            scope_report, "live_session_ids", lambda cwd=None: frozenset()
        )
        classification = scope_report._classify_denied_path("shared.py", offer, [])
        assert classification == "claimed by session other (liveness not checked)"


# ---------------------------------------------------------------------------
# (b) session.scope_report op
# ---------------------------------------------------------------------------


class TestScopeReportOp:
    def test_op_returns_compute_offer_shape_for_explicit_session_id(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        result = _handler({"session_id": "mine", "cwd": str(repo)})
        expected = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert result == expected

    def test_op_resolves_calling_session_when_session_id_omitted(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

        result = _handler({"cwd": str(repo)})
        assert result["session_id"] == "mine"

    def test_op_unresolvable_session_id_returns_error_envelope_not_raise(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = _handler({"cwd": str(repo)})
        assert result["session_id"] == ""
        assert result["safe_paths"] == []
        assert "error" in result

    def test_op_mutates_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        before = _dirty_status(repo)
        _handler({"session_id": "mine", "cwd": str(repo)})
        after = _dirty_status(repo)
        assert before == after


# ---------------------------------------------------------------------------
# (c) safe_commit_offer post-commit residue report (C3, 2026-08-05
# engine-ops-declare-what-they-write plan) — REPORT-ONLY, never a gate.
# ---------------------------------------------------------------------------


class TestPostCommitResidueReport:
    """Proves AC3/AC4/AC5: after `auto_commit_session` lands a session's own
    commit groups, a fresh `git status --porcelain` re-read still names any
    dirty path left behind, grouped by top-level `state/` class — but a path
    a live PEER session owns renders as owned, never as this session's own
    residue (the exact harm case AC5 exists to prevent: nudging an operator
    into sweeping a peer's in-flight work)."""

    def test_residue_names_uncommitted_path_and_attributes_peer_owned_path(
        self, tmp_path
    ):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))

        # "mine"'s own claimed, committable file.
        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        # A live PEER's in-flight claim — stays dirty after "mine" commits,
        # and must be attributed to "other", never rendered as residue.
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        # A genuinely unclaimed dirty file nobody's touched.txt names — the
        # engine-op-with-bare-open() shape the plan's Problem section
        # describes. Under a state/<class> path so grouping is observable.
        leftover_dir = repo / "state" / "leftover"
        leftover_dir.mkdir(parents=True)
        (leftover_dir / "residue.txt").write_text("left behind")

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        # Sanity: only "mine.py" actually committed.
        assert len(report["groups"]) == 1
        assert report["groups"][0]["paths"] == ["mine.py"]
        assert report["groups"][0]["committed"] is True

        status_after = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "peer.py" in status_after
        assert "residue.txt" in status_after

        residue = report["residue"]
        residue_paths = {p for paths in residue.values() for p in paths}

        # AC3: the leftover, never-committed path is named as residue.
        assert "state/leftover/residue.txt" in residue_paths
        assert "state/leftover" in residue

        # AC5: the peer-owned path is NOT residue — it is attributed away
        # via offer["excluded"] before residue is computed.
        assert "peer.py" not in residue_paths
        assert any(
            e["path"] == "peer.py" and e["reason"] == "owned by session other"
            for e in report["excluded"]
        )

        # AC4: report-only — the commit landed successfully regardless of
        # non-empty residue, nothing here staged/blocked anything.
        assert report["failed_groups"] == []

        rendered = safe_commit_offer._render_report(report, str(repo))
        assert "state/leftover" in rendered
        assert "residue.txt" in rendered or "1 file(s)" in rendered
        # The peer-owned path must not be named inline under the residue
        # section text (it is covered by the pre-existing excluded summary
        # instead, not duplicated as residue).
        assert "peer.py" not in rendered

    def test_residue_rendering_bounded_across_many_classes(self, tmp_path):
        """Review: code-reviewer (Finding 1/Finding 5) regression guard —
        many repo-root residue files, each its own class per
        `_residue_class`, must NOT render one line per class. Direct
        perturbation guard for the pinned
        `test_outcome_is_last_and_output_does_not_scale_with_excluded_count`
        regression: reverting the `_RESIDUE_CLASS_PREVIEW_COUNT` cap in
        `_render_report` makes this fail."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        # 60 repo-root orphan files, none claimed by any session -- each
        # becomes its OWN residue class (bare top-level segment, per
        # `_residue_class`), reproducing the exact fixture shape from the
        # pinned outcome-signal test.
        for i in range(60):
            (repo / ("orphan_%03d.py" % i)).write_text("o")

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))
        residue = report["residue"]
        assert len(residue) > safe_commit_offer._RESIDUE_CLASS_PREVIEW_COUNT

        rendered = safe_commit_offer._render_report(report, str(repo))
        lines = rendered.splitlines()
        assert len(lines) < 30, (
            "residue rendering must not grow one line per class -- aggregate "
            "beyond the preview cap into a '... and N more class(es)' tail"
        )
        assert "more class(es)" in rendered

    def test_residue_class_sample_capped_with_many_paths_in_one_class(
        self, tmp_path
    ):
        """The per-class sample stays capped at `_RESIDUE_CLASS_SAMPLE_COUNT`
        even when one class holds many more paths than that."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        leftover_dir = repo / "state" / "leftover"
        leftover_dir.mkdir(parents=True)
        for i in range(10):
            (leftover_dir / ("residue_%02d.txt" % i)).write_text("left behind")

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))
        residue = report["residue"]
        assert "state/leftover" in residue
        assert len(residue["state/leftover"]) == 10

        rendered = safe_commit_offer._render_report(report, str(repo))
        class_line = next(
            line for line in rendered.splitlines() if line.strip().startswith("state/leftover:")
        )
        sample_count = class_line.count("residue_")
        assert sample_count == safe_commit_offer._RESIDUE_CLASS_SAMPLE_COUNT
        assert "+7 more" in class_line


# ---------------------------------------------------------------------------
# (d) compute_offer's four-bucket ownership readout (C5, 2026-08-05
# in-process-writers-declare-their-writes plan) — EXTENDS the C3 residue
# report above, does not replace it. mine / named peer / unattributed /
# degraded; see safe_commit_offer.OwnershipReadout's own docstring for the
# bucket contract.
# ---------------------------------------------------------------------------


class TestOwnershipReadout:
    def test_mine_bucket_mirrors_safe_paths(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        ownership = offer["ownership"]
        assert ownership["mine"] == offer["safe_paths"] == ["a.py"]
        assert ownership["degraded"] is False

    def test_peer_bucket_names_a_live_peers_claim(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        ownership = offer["ownership"]
        assert ownership["degraded"] is False
        assert "shared.py" not in ownership["mine"]
        assert "shared.py" not in ownership["unattributed"]
        entry = next(p for p in ownership["peer"] if p["path"] == "shared.py")
        assert entry["owner"] == "other"
        assert entry["claim_source"] in ("session", "agent", "agent-race")
        assert entry["liveness"] in ("live", "dead", "undetermined")

    def test_unattributed_bucket_names_an_unclaimed_dirty_path(self, tmp_path):
        """DR-258 (ScopeResult.orphans's own docstring): a dirty path nobody
        ever claimed lands here, never in `peer` (there is no owner to
        stand behind) and never adopted into `mine`."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        ownership = offer["ownership"]
        assert ownership["degraded"] is False
        assert "orphan.py" not in ownership["mine"]
        assert "orphan.py" not in {p["path"] for p in ownership["peer"]}
        assert "orphan.py" in ownership["unattributed"]

    def test_degraded_forces_every_non_mine_path_into_unattributed(
        self, tmp_path, monkeypatch
    ):
        """Perturbation-proof of the peer-vs-unattributed distinction (AC7):
        make one peer's claim set unreadable this call, and prove an
        OTHERWISE-resolvable peer claim (`other_claimed.py`, contrast with
        `test_peer_bucket_names_a_live_peers_claim` above, where the exact
        same claim shape lands in `peer`) is denied that attribution and
        folded into `unattributed` instead -- this call cannot stand behind
        ANY owner name it would otherwise print, not just the one it could
        not read.

        NOT `os.chmod(path, 0o000)`: Windows does not honour POSIX mode
        bits for the owning user, so the chmod'd file stays readable there
        and the test's own precondition (an unreadable touched.txt) never
        establishes itself -- `compute_offer` then correctly reports
        ``degraded is False``, and the test goes red for a reason that has
        nothing to do with the code under test (see
        state/bug-backlog/2026-08-07-ownership-degraded-test-uses-posix-only-chmod-and-is-red-on-windows.yaml).
        Instead, monkeypatch the exact read seam `compute_scope` uses for a
        peer's ``touched.txt`` (``pathlib.Path.read_text``) to raise
        `PermissionError` for THIS peer's file only, leaving every other
        session's ``touched.txt`` read in this fixture (``mine``,
        `other`) untouched -- the same platform-neutral simulated failure
        on Windows and POSIX alike, pinning the identical property either
        way."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        core.init("peer", cwd=str(repo))

        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        (repo / "other_claimed.py").write_text("s")
        scope.touch("other", "other_claimed.py", cwd=str(repo))

        peer_touched = Path(core.session_dir("peer", cwd=str(repo))) / "touched.txt"
        peer_touched.write_text("unreadable.md\n", encoding="utf-8")
        (repo / "unreadable.md").write_text("peer's own in-flight work")

        real_read_text = Path.read_text

        def _read_text_deny_peer_only(self, *args, **kwargs):
            if self == peer_touched:
                raise PermissionError(
                    f"simulated unreadable touched.txt: {self}"
                )
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text_deny_peer_only)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        ownership = offer["ownership"]
        assert ownership["degraded"] is True
        assert ownership["peer"] == []
        # `mine` always mirrors `safe_paths` verbatim -- this call's own
        # fail-closed withhold of uncontested candidates (see the captured
        # stderr diagnostic) is compute_scope's pre-existing behavior, not
        # something this bucket adds or corrects; it is asserted equal here,
        # not asserted non-empty, so a change to that pre-existing withhold
        # does not make this test lie about what `ownership` promises.
        assert ownership["mine"] == offer["safe_paths"]
        assert "other_claimed.py" in ownership["unattributed"]
        assert "unreadable.md" in ownership["unattributed"]

    def test_ownership_key_is_additive_existing_keys_unchanged(self, tmp_path):
        """WIRE SHAPE (C5): `ownership` is an ADDED key -- every pre-existing
        `SafeCommitOffer` key stays byte-identical in shape, since two live
        sibling plans consume this shape verbatim (see the plan's § Cross-
        plan coordination)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert set(offer.keys()) == {
            "session_id",
            "safe_paths",
            "excluded",
            "orphans",
            "indeterminate",
            "ownership",
        }
        assert set(offer["ownership"].keys()) == {"mine", "peer", "unattributed", "degraded"}


class TestClassificationOffer:
    """The 2026-08-07 classification-offer fix (bug-backlog `2026-08-06-
    scoped-commit-denial-names-unclassified-for-a-peer-held-path`).

    `compute_offer`'s candidate set is the calling session's own claim plus
    its own sub-agent fan-out -- never the pathspec the caller named. A path
    the caller never touched, whose mtime predates its `started_at`, is
    therefore not a `compute_scope` candidate at all: Step 3/3b's peer-claim
    read is never consulted for it, Step 4 mints no `skipped` entry naming
    its holder, and Step 5 keeps it out of `orphans` (`other_owner` has it).
    It is absent from every key of the offer, so `_classify_denied_path` fell
    through to `_CLASSIFICATION_UNCLASSIFIED` for a path a live peer
    demonstrably held -- pointing an operator at `include_orphans: true`, a
    remedy that provably cannot help a peer-claimed path.

    Negative-spec: these tests pin the WORDING of a denial and, in
    `test_classification_offer_never_widens_the_verdict`, the fact that the
    verdict itself is untouched. Nothing here may be read as licence to feed
    the classification offer back into the allow-list -- see
    `scope_report`'s "TWO OFFERS" module-docstring paragraph.
    """

    @staticmethod
    def _started_at_in_the_future(repo, session_id):
        """Push `session_id`'s `started_at` past now, so a file dirtied after
        this call never satisfies `compute_scope` Step 2's `mtime >=
        started_at_epoch` test and never becomes a candidate for it. Same
        recipe as `TestOrphanAdoptionPositiveEvidence`'s non-candidate probe
        above -- the shape the live reproduction hit."""
        from datetime import datetime, timezone

        sdir = Path(core.session_dir(session_id, cwd=str(repo)))
        future = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
        )
        (sdir / "started_at").write_text(
            future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
        )

    def test_peer_held_path_never_touched_by_caller_names_the_holder(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        self._started_at_in_the_future(repo, "mine")

        (repo / "peer-held.md").write_text("peer's in-flight work")
        scope.touch("peer", "peer-held.md", cwd=str(repo))

        # The defect's own premise, asserted rather than assumed: the path is
        # absent from EVERY key of the caller's primary offer.
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "peer-held.md" not in offer["safe_paths"]
        assert "peer-held.md" not in (offer["orphans"] or [])
        assert "peer-held.md" not in {e["path"] for e in offer["excluded"]}

        ok, reason = assert_paths_in_session_scope(
            "mine", ["peer-held.md"], cwd=str(repo)
        )

        assert ok is False
        assert scope_report.deny_reason_names_a_holder(reason) is True
        assert "peer" in reason
        assert scope_report._CLASSIFICATION_UNCLASSIFIED not in reason

    def test_orphan_still_classifies_as_orphan_and_still_denies(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        self._started_at_in_the_future(repo, "mine")

        (repo / "orphan.md").write_text("nobody's")

        ok, reason = assert_paths_in_session_scope(
            "mine", ["orphan.md"], cwd=str(repo), allow_orphans=False
        )

        # The regression that matters is the BOOLEAN, not the string: the
        # classification offer adopts a caller-named unclaimed path into its
        # OWN safe_paths by construction, so a verdict read off it would
        # allow here.
        assert ok is False
        assert scope_report._CLASSIFICATION_ORPHAN in reason
        # Deliberately the PREDICATE, not `CLAIMED_BY_PREFIX not in reason`:
        # the orphan wording reads "...dirty but claimed by no session" and
        # contains that prefix mid-sentence while meaning its inverse. See
        # `CLAIMED_BY_PREFIX`/`CLAIMED_BY_SENTINELS` for the collision.
        assert scope_report.CLAIMED_BY_PREFIX in reason
        assert scope_report.deny_reason_names_a_holder(reason) is False

    def test_orphan_classification_is_unreachable_with_include_orphans(self, tmp_path):
        """`_CLASSIFICATION_ORPHAN` contains the literal `CLAIMED_BY_PREFIX`
        substring, and `scoped_git_commit._include_orphans_ineffective_note`
        discriminates on that substring -- but only ever on a denial where
        `include_orphans` was True. The two are disjoint BY CONSTRUCTION, and
        this pins it: with adoption requested, a verified caller has every
        orphan ALLOWED (so it is never classified at all), and an unverified
        one gets `_CLASSIFICATION_INCLUDE_ORPHANS_IGNORED`, which carries no
        claimed-by wording. Reachable `_CLASSIFICATION_ORPHAN` therefore
        implies `include_orphans` was False, where the note is silent anyway.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        self._started_at_in_the_future(repo, "mine")
        (repo / "orphan.md").write_text("nobody's")

        # Verified caller (core.init wrote meta.json) -- adoption succeeds,
        # so there is no denial to word at all.
        ok, _reason = assert_paths_in_session_scope(
            "mine", ["orphan.md"], cwd=str(repo), allow_orphans=True
        )
        assert ok is True

        # Unverified caller (no session dir, hence no meta.json) -- denial
        # names the unmet ASK, never a holder.
        ok, reason = assert_paths_in_session_scope(
            "never-initialized", ["orphan.md"], cwd=str(repo), allow_orphans=True
        )
        assert ok is False
        assert scope_report._CLASSIFICATION_ORPHAN not in reason
        assert scope_report.deny_reason_names_a_holder(reason) is False

    def test_genuinely_unknown_path_stays_unclassified(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        # Clean at HEAD and named by no session anywhere -- there is nothing
        # for either offer to say about it, and saying nothing is correct.
        ok, reason = assert_paths_in_session_scope(
            "mine", ["README.md"], cwd=str(repo)
        )

        assert ok is False
        assert scope_report._CLASSIFICATION_UNCLASSIFIED in reason
        assert scope_report.deny_reason_names_a_holder(reason) is False

    def test_classification_offer_never_widens_the_verdict(self, tmp_path):
        """THE invariant test. `extra_candidates` feeds `compute_scope` Step
        1, so every path below IS a member of the classification offer's own
        `safe_paths` -- adopted because the caller named it, not because the
        caller owns it. If someone later "simplifies" the two `compute_offer`
        calls into one, this allows, and any caller can own any dirty path in
        the tree by putting it in its own pathspec.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        self._started_at_in_the_future(repo, "mine")

        named = ["unclaimed-a.md", "unclaimed-b.md"]
        for path in named:
            (repo / path).write_text("dirty, claimed by nobody")

        classification_offer = safe_commit_offer.compute_offer(
            "mine", cwd=str(repo), extra_candidates=named
        )
        assert set(named) <= set(classification_offer["safe_paths"])

        ok, _reason = assert_paths_in_session_scope(
            "mine", named, cwd=str(repo), allow_orphans=False
        )
        assert ok is False

    def test_default_compute_offer_is_byte_for_byte_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        assert safe_commit_offer.compute_offer(
            "mine", cwd=str(repo)
        ) == safe_commit_offer.compute_offer(
            "mine", cwd=str(repo), extra_candidates=None
        )
