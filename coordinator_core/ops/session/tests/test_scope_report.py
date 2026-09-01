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
  - Does NOT assert anything about block_subagent_commit's OWN behavior
    (its message-cap logic, its branch selection) — that stays C4b's own
    coverage. `test_indeterminate_call_names_the_degradation` below is the
    one deliberate exception: it imports block_subagent_commit's
    `_ownership_leg_summary` read-only, as a fixed downstream consumer, to
    pin THIS module's own contract (`scope_report.py`'s
    `_CLASSIFICATION_INDETERMINATE` comment, "TRUNCATION IS EXPECTED AND
    CORRECT HERE") — that the discriminating token survives truncation at
    the seam. It asserts nothing about block_subagent_commit's own logic
    beyond that one pinned byte budget.
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
from coordinator_core.session import claim_index, core, scope
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# Real git spawn is load-bearing: assert_paths_in_session_scope and
# session.scope_report both delegate to compute_offer, which reads real
# `git status`/diff output to compute the dirty set no mock stands in for.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

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
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(
        ["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    return tmp_path


def _dirty_status(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
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

    def test_claim_index_error_denies(self, tmp_path, monkeypatch):
        # The ownership leg's source is `claim_index.classify_paths` since the
        # 2026-08-21 rebuild; ANY error beneath it still denies rather than
        # falling through to an empty-and-therefore-permissive answer.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        def _boom(session_id, paths, sessions_dir=None, cwd=None):
            raise RuntimeError("simulated claim-index failure")

        monkeypatch.setattr(scope_report.claim_index, "classify_paths", _boom)

        ok, reason = assert_paths_in_session_scope("mine", ["a.py"], cwd=str(repo))
        assert ok is False
        assert "simulated claim-index failure" in reason

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
        # the same cross-repo miss the DoE-claude incident hit against a real
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
        # The pin stops at the DISCRIMINATOR, not at a closing paren. The
        # paren was only ever adjacent because `orphan` was the one
        # classification carrying no remedy; its siblings (`unclaimed`,
        # `indeterminate`) have appended `_REMEDY_WHO_CLAIMS` inside these
        # same parens for as long as they have existed. Pinning the paren
        # made this guard fail on a message that got MORE useful, which is
        # the opposite of what a stable-prefix guard is for.
        assert reason.startswith(
            "path outside session mine scope: 'orphan.py' "
            "(orphan — no session holds a claim on it"
        )
        # The remedy is present and, per the constant's own note, comes after
        # the discriminator so a truncating reader keeps the part it needs.
        assert "who-claims-path" in reason
        assert reason.index("orphan —") < reason.index("who-claims-path")

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

        def _boom(session_id, paths, sessions_dir=None, cwd=None):
            raise RuntimeError("simulated claim-index failure")

        monkeypatch.setattr(scope_report.claim_index, "classify_paths", _boom)

        ok_default, _ = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok_default is False
        ok_allow, _ = assert_paths_in_session_scope(
            "mine", ["orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok_allow is False

    def test_answer_not_covering_the_path_denies_in_both_modes(
        self, tmp_path, monkeypatch
    ):
        # Post-rebuild analogue of the missing-`orphans`-key shape: an answer
        # that simply has no entry for a path must never read as "unclaimed,
        # therefore adoptable". A path the answer does not cover is
        # unanswerable, and unanswerable denies -- in BOTH modes, so
        # `allow_orphans` cannot turn a gap in the answer into a permission.
        repo = self._seed_orphan(tmp_path)

        def _covers_nothing(session_id, paths, sessions_dir=None, cwd=None):
            return scope_report.claim_index.OwnershipAnswer(
                by_path={}, complete=True, abort_cause=None
            )

        monkeypatch.setattr(
            scope_report.claim_index, "classify_paths", _covers_nothing
        )

        ok_default, _ = assert_paths_in_session_scope("mine", ["orphan.py"], cwd=str(repo))
        assert ok_default is False
        ok_allow, _ = assert_paths_in_session_scope(
            "mine", ["orphan.py"], cwd=str(repo), allow_orphans=True
        )
        assert ok_allow is False

    def test_r1_non_candidate_path_with_unreadable_peer_claim_still_denies(
        self, tmp_path, monkeypatch
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
        (repo / "peer.md").write_text("peer's own in-flight work")
        # Claimed through the real writer, NOT a hand-written line. The
        # pre-rebuild fixture wrote a bare `peer.md\n` -- the legacy
        # verb-less touched.txt shape `scope.parse_touch_event` still accepts
        # and `claim_index` deliberately does not (see `_last_verb_per_path`:
        # "a NEW reader with no legacy bare-path-line compatibility
        # obligation"). Against the rebuilt leg that fixture claimed nothing,
        # so the test proved nothing; on-disk census 2026-08-21 found the
        # bare shape only under `.archive/`, which the walk never reaches.
        scope.touch("peer", "peer.md", cwd=str(repo))
        # Unreadability is INJECTED AT THE READER, not by chmod-ing a
        # `touched.txt`. Two reasons the old fixture stopped working: the sink
        # is `touch-record.jsonl` now (so the chmod target did not exist and
        # raised FileNotFoundError before the assertion ran), and `os.chmod`
        # with 0o000 does not make a file unreadable to its owner on Windows
        # at all -- this repo is Windows-first, so even a corrected filename
        # would not have established the precondition here.
        peer_dir = os.path.normcase(core.session_dir("peer", cwd=str(repo)))
        real_reader = claim_index._read_stream_claims

        def _unreadable(sink_path):
            if os.path.normcase(str(sink_path)).startswith(peer_dir):
                return {}, False
            return real_reader(sink_path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)
        ok, reason = assert_paths_in_session_scope(
            "mine", ["peer.md"], cwd=str(repo), allow_orphans=True
        )

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
    reason field (break-class fix, 2026-08-07 — DoE-claude report). Three
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
        `assert_paths_in_session_scope` (already used for the ownership leg's
        own claim-index read and the positive-evidence check) must be the same
        one the liveness oracle answers for — they must not disagree about
        WHICH repo."""
        monkeypatch.setattr(
            scope_report.claim_index,
            "classify_paths",
            lambda session_id, paths, sessions_dir=None, cwd=None: (
                scope_report.claim_index.OwnershipAnswer(
                    by_path={
                        "shared.py": scope_report.claim_index.PathOwnership(
                            verdict=scope_report.claim_index.OWNERSHIP_PEER,
                            peers=["other"],
                        )
                    },
                    complete=True,
                    abort_cause=None,
                )
            ),
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
    """Proves AC3/AC4/AC5: after `commit_session_offer` lands a session's own
    commit groups, a fresh `git status --porcelain` re-read still names any
    dirty path left behind, grouped by top-level `state/` class — but a path
    a live PEER session owns renders as owned, never as this session's own
    residue (the exact harm case AC5 exists to prevent: nudging an operator
    into sweeping a peer's in-flight work)."""

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
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

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

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
            **no_console_creationflags(),
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

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
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

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        residue = report["residue"]
        assert len(residue) > safe_commit_offer._RESIDUE_CLASS_PREVIEW_COUNT

        rendered = safe_commit_offer._render_report(report, str(repo))
        lines = rendered.splitlines()
        assert len(lines) < 30, (
            "residue rendering must not grow one line per class -- aggregate "
            "beyond the preview cap into a '... and N more class(es)' tail"
        )
        assert "more class(es)" in rendered

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
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

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
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

    def test_an_unclaimed_path_lands_in_no_bucket_at_all(self, tmp_path):
        """DR-258 said a dirty path nobody ever claimed must never be adopted
        into `mine` and never invented into `peer`, and put it in
        `unattributed` so it was at least SEEN. Post-2026-08-21 it is in no
        bucket: `compute_offer` reads the claim ledger and nothing else, so a
        file nobody claimed is not a thing this answer knows about.

        The DR-258 constraint itself is untouched and is what this still pins
        -- not adopted, no owner invented. What changed is that the honest
        answer is now silence rather than a named-but-unattributed entry, and
        the surface that DOES enumerate dirty-but-unclaimed paths is the
        post-commit residue report, which takes its own worktree read.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        ownership = offer["ownership"]

        assert ownership["degraded"] is False
        assert "orphan.py" not in ownership["mine"]
        assert "orphan.py" not in {p["path"] for p in ownership["peer"]}
        assert "orphan.py" not in ownership["unattributed"]

    def test_degraded_forces_every_contested_path_into_unattributed(
        self, tmp_path, monkeypatch
    ):
        """AC7's binding requirement, perturbation-proved: make ONE claimant's
        file unreadable and an OTHERWISE-resolvable peer claim is denied
        attribution too -- this call cannot stand behind ANY owner name it
        would print, not merely the one it could not read.

        The read seam moved with the 2026-08-21 rebuild. It is no longer
        `pathlib.Path.read_text` (which `compute_scope` used) but
        `claim_index._read_lines_discard_torn_tail`, the claim index's own
        reader, which reports unreadability as a `(lines, ok)` pair rather
        than by raising. NOT `os.chmod(path, 0o000)` either way: Windows does
        not honour POSIX mode bits for the owning user, so the chmod'd file
        stays readable and the test's own precondition never establishes
        itself -- the test then goes red for a reason unrelated to the code
        under test (state/bug-backlog/2026-08-07-ownership-degraded-test-uses-
        posix-only-chmod-and-is-red-on-windows.yaml).
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        core.init("unreadable-peer", cwd=str(repo))
        (repo / "other_claimed.py").write_text("x")
        scope.touch("mine", "other_claimed.py", cwd=str(repo))
        scope.touch("other", "other_claimed.py", cwd=str(repo))
        (repo / "hidden.py").write_text("y")
        scope.touch("unreadable-peer", "hidden.py", cwd=str(repo))

        # The unreadable-claim seam moved twice and this patch target moved
        # with it. `_read_lines_discard_torn_tail` was DELETED by the
        # 2026-08-21 rebuild; the reader is now
        # `claim_index._read_stream_claims(sink) -> (claims, content_read_ok)`.
        # Blind on the peer's session DIRECTORY rather than a `touched.txt`
        # filename: the sink dialect is `touch-record.jsonl` now, and matching
        # the old filename would silently patch nothing, leaving the
        # precondition unestablished and the test green for the wrong reason.
        blinded_dir = os.path.normcase(
            core.session_dir("unreadable-peer", cwd=str(repo))
        )
        real_reader = claim_index._read_stream_claims

        def _unreadable(sink_path):
            if os.path.normcase(str(sink_path)).startswith(blinded_dir):
                return {}, False
            return real_reader(sink_path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        ownership = offer["ownership"]

        assert ownership["degraded"] is True
        assert ownership["peer"] == []
        assert "other_claimed.py" in ownership["unattributed"]
# ---------------------------------------------------------------------------
# TestClassificationOffer -- DELETED 2026-08-21 with the shape it pinned.
#
# It covered the TWO-OFFER split: a second `compute_offer` computed with the
# caller's own pathspec as `extra_candidates`, safe to read for WORDING and
# catastrophic to read as a verdict, kept apart from the primary offer by
# discipline alone. `assert_paths_in_session_scope` now asks
# `claim_index.classify_paths` about the pathspec directly, so there is no
# second offer to widen anything and `extra_candidates` no longer exists as a
# parameter. Tests of a deleted parameter are not coverage; the negative spec
# that replaced them lives on `compute_offer` and in this module's own
# docstring, and `TestOwnershipLegRebuilt` below pins the behaviour.
# ---------------------------------------------------------------------------


class TestOwnershipLegRebuilt:
    """The 2026-08-21 rebuild's own acceptance criteria: the ownership leg is
    back in force, it is zero-spawn, and it names every holder it refuses
    for."""

    def test_the_stand_down_text_is_gone_from_the_source(self):
        # The suspension was a NAMED, temporary loss of a safety property.
        # Leaving its text behind after the property is restored is how a
        # reader concludes the gate is still off. `_MECHANISM_DISABLED` is
        # `safe_commit_offer`'s own and outlives this leg -- this pin is about
        # the OWNERSHIP leg's stand-down only.
        src = Path(scope_report.__file__).read_text(encoding="utf-8")
        assert "STOOD DOWN" not in src
        assert "_MECHANISM_DISABLED" not in src

    def test_a_peers_path_is_refused_and_the_holder_is_named(self, tmp_path):
        # The acceptance criterion in one line: a dispatched committer cannot
        # commit a path another session owns. Between `e927d9463` and the
        # rebuild it could.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "theirs.py").write_text("peer's in-flight work")
        scope.touch("peer", "theirs.py", cwd=str(repo))

        ok, reason = assert_paths_in_session_scope(
            "mine", ["theirs.py"], cwd=str(repo)
        )

        assert ok is False
        assert scope_report.deny_reason_names_a_holder(reason)
        assert "peer" in reason

    def test_a_path_two_peers_hold_names_both(self, tmp_path):
        # `_classify_denied_path` can name only ONE holder (its earned-liveness
        # wording needs a bare sid). The rest must not vanish from the refusal
        # -- this is the message an operator uses to decide who to go talk to.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer-a", cwd=str(repo))
        core.init("peer-b", cwd=str(repo))
        (repo / "contested.py").write_text("two holders")
        scope.touch("peer-a", "contested.py", cwd=str(repo))
        scope.touch("peer-b", "contested.py", cwd=str(repo))

        ok, reason = assert_paths_in_session_scope(
            "mine", ["contested.py"], cwd=str(repo)
        )

        assert ok is False
        assert "peer-a" in reason
        assert "peer-b" in reason

    def test_the_gate_spawns_no_subprocess(self, tmp_path, monkeypatch):
        # This runs on the COMMIT HOT PATH. The mechanism it replaces cost 73
        # processes and 5,609ms of CPU per call, paid by every
        # dispatched-committer invocation.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))
        # Warm every cached git-backed resolution (sessions_dir) BEFORE the
        # trap, so what is measured is this call's own spawns, not a cold
        # cache's.
        assert assert_paths_in_session_scope("mine", ["a.py"], cwd=str(repo))[0] is True

        def _explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("the ownership gate spawned a subprocess")

        monkeypatch.setattr(subprocess, "run", _explode)
        monkeypatch.setattr(subprocess, "Popen", _explode)
        monkeypatch.setattr(subprocess, "check_output", _explode)

        ok, _ = assert_paths_in_session_scope("mine", ["a.py"], cwd=str(repo))
        assert ok is True

        denied_ok, _ = assert_paths_in_session_scope(
            "mine", ["nobody-touched-this.py"], cwd=str(repo)
        )
        assert denied_ok is False

    def test_indeterminate_call_names_the_degradation(self, tmp_path, monkeypatch):
        # Review: coordinator:code-reviewer, coordinatorcode-reviewer.a8583fd1571c29519
        # (P2) — scope_report.py's `_CLASSIFICATION_INDETERMINATE` comment
        # asserts "TRUNCATION IS EXPECTED AND CORRECT HERE ... the capped
        # path still carries 'indeterminate'/'adoption withheld'" and cited
        # this exact test name as the thing that pins it. No such test
        # existed. Written here to make the comment true, and to prove the
        # claim itself: the discriminating token DOES survive
        # block_subagent_commit._ownership_leg_summary's ~70-byte cap.
        from coordinator_core.bash_guards.block_subagent_commit import (
            _ownership_leg_summary,
        )
        from coordinator_core.session import claim_index as ci

        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.txt").write_text("dirty")

        def _degraded_classify(session_id, paths, sessions_dir=None, cwd=None):
            # An aborted walk: no path resolves to MINE or UNCLAIMED, and
            # `complete=False` is exactly what sets `call_indeterminate` in
            # `assert_paths_in_session_scope` (scope_report.py, "An aborted
            # walk is this gate's `indeterminate`").
            return ci.OwnershipAnswer(by_path={}, complete=False, abort_cause="io_error")

        monkeypatch.setattr(claim_index, "classify_paths", _degraded_classify)

        ok, reason = assert_paths_in_session_scope(
            "mine", ["a.txt"], cwd=str(repo)
        )

        assert ok is False
        assert "indeterminate" in reason
        assert "adoption withheld" in reason

        capped = _ownership_leg_summary(reason)
        assert len(capped.encode("utf-8")) <= 73  # 70-byte cap + "..." ellipsis
        assert "indeterminate" in capped
        assert "adoption withheld" in capped
