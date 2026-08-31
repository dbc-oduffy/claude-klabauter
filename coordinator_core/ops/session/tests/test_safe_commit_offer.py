"""
coordinator_core.ops.session.tests.test_safe_commit_offer

Tests for coordinator_core.ops.session.safe_commit_offer — the unattended
stop-event auto-commit+push mechanism (compute_offer, path normalization,
commit_session_offer, and the CLI main()).

Coverage:
  (a) compute_offer — own-touched-safe, other-session-excluded-with-reason,
      unclaimed-dirty-since-start-is-declined (DR-246, superseded from an
      earlier "is-safe" disposition), orphan-before-start-excluded,
      empty-result-is-valid, required-session-id, read-only-no-mutation.
  (b) exact-vs-broadened — `_resolve_agent_touched_candidates` uses "exact"
      mode: a sibling EM's own sub-agent fan-out is NEVER unioned in, even
      though "broadened" mode would include it (regression guard for the
      2026-07-31 finding: broadened returns an identical union per session,
      not per-session attribution).
  (c) path normalization — `_normalize_agent_touched_entry`: POST-C2, a
      repo-relative entry passes through unchanged; a cross-repo or
      out-of-repo (`../`-escaping) entry is dropped (returns None); a
      directory entry keeps its trailing slash.
  (d) directory-entry expansion — `_dirty_files_under` finds dirty files
      under a given subdirectory via git, ignores files outside it.
  (e) commit_session_offer — NO confirmation step (direct call lands a real
      commit); default mechanical grouping; explicit `--message` single
      group; explicit `groups` never widens past computed safe_paths (a
      peer-owned path smuggled into a caller-supplied group is dropped, not
      committed); empty safe_paths is a valid no-op ("nothing to commit").
  (f) CLI — --dry-run (no commit), --message, --groups-json, mutually
      exclusive flags, unresolvable-session exit 1, usage exit 2.

Class B: substrate is untracked .git/coordinator-sessions/ — commit
assertions here ARE made (commit_session_offer is the one mutating surface
this module owns), scoped to the test's own tmp_path fixture repo, never
the real corpus.

Spec backlink: coordinator_core/ops/session/safe_commit_offer.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.git.commit import CommitOutcome, CommitRefused
from coordinator_core.ipc import dispatch_message
from coordinator_core.session import claim_index, core, scope, touch_record
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# Real git spawn is load-bearing: compute_offer's dirty-set math and
# `_dirty_files_under` read actual `git status`/diff output, and
# commit_session_offer lands real commits under test — no mock stands in for
# git's own path-classification and index state here.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    # Review: staff-eng F12 — check=True on every fixture-setup git call
    # (mirrors test_scope.py's _make_repo): a silent fixture-setup failure
    # must not masquerade as a passing test.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _agent_claim(agent_dir, *paths, owner_sid=None, ts=None):
    """Record an agent dir's claims in the one dialect its readers read.

    A bare-path ``touched.txt`` stopped being a claim surface when the compat
    union came out (2026-08-26), so a fixture that writes one claims nothing.
    A ``'<verb> <ts> <path>'`` journal line is decoded into a real event, which
    is how these tests express release ordering.

    The journal-vs-bare-path choice is a shape-sniff, not a flag: a path that
    itself parses as ``<T|R> <ISO-ts> <path>`` would be reinterpreted as a
    journal line. No fixture path has that shape; pass an explicit verb if one
    ever does.

    ``ts`` (C5, docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-
    agent.md) -- an explicit epoch-seconds float threaded onto every event
    this call writes, letting a test place a claim's ``edit_ts`` inside or
    outside ``liveness._ABANDONMENT_WINDOW_SEC`` deterministically instead of
    depending on wall-clock "now". ``None`` (default) records "now", same as
    every pre-C5 caller of this helper.
    """
    agent_dir = Path(agent_dir)
    if owner_sid is None:
        backptr = agent_dir / "em-session-id.txt"
        owner_sid = (
            backptr.read_text(encoding="utf-8").splitlines()[0].strip()
            if backptr.is_file()
            else agent_dir.name
        )
    sink = agent_dir / scope._TOUCH_RECORD_FILENAME
    for entry in paths:
        verb, _ts, parsed = scope.parse_touch_event(entry)
        if verb in (touch_record.VERB_TOUCH, touch_record.VERB_RELEASE) and parsed != entry:
            path, event_verb = parsed, verb
        else:
            path, event_verb = entry, touch_record.VERB_TOUCH
        touch_record.append_event(
            sink,
            session_id=owner_sid,
            agent_id=agent_dir.name,
            verb=event_verb,
            path=path,
            timestamp=ts,
        )
    return sink


def _agent_dir(repo, aid):
    d = Path(core.sessions_dir(cwd=str(repo))) / ".agents" / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# (a) compute_offer
# ---------------------------------------------------------------------------


class TestComputeOffer:
    def test_own_touched_files_are_safe(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("mine", "b.py", cwd=str(repo))
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert set(offer["safe_paths"]) == {"a.py", "b.py"}
        assert offer["excluded"] == []
        assert offer["session_id"] == "mine"

    def test_other_session_file_excluded_with_owner_reason(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        # The claim only contests a path with uncommitted content — a peer's
        # touch record for a clean path is stale by construction and is pruned
        # (compute_scope Step 3). Write the file so the contention is real.
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "shared.py" not in offer["safe_paths"]
        assert {"path": "shared.py", "reason": "owned by session other"} in offer["excluded"]

    def test_a_recently_touched_agent_claim_is_withheld_as_in_flight(
        self, tmp_path
    ):
        """CORRECTED (C5, docs/plans/2026-08-27-safe-commit-offer-excludes-
        a-live-agent.md): C3's own fix was INERT in production -- an agent
        id is NEVER a member of ``liveness.live_session_ids`` (that set
        walks session dirs only, ``.agents`` is explicitly excluded), so the
        live-set check C3 shipped folded every agent-attributed claim back
        into ``safe_paths`` unconditionally, always, on the real tree. This
        test asserts the REPLACEMENT mechanism instead: recency against
        ``liveness._ABANDONMENT_WINDOW_SEC``. ``aid-recent`` here is given
        NO session dir of its own at all (unlike C3's fixture) -- an agent id
        is never a `live_session_ids` member in production and this fixture
        does not pretend otherwise. Its claim's ``edit_ts`` is set to 60s
        ago, well inside the abandonment window.

        The corrected behaviour: a claim touched inside the window is
        withheld from ``safe_paths`` (same as a contested peer path), named
        in ``excluded`` with an operator-actionable reason, and surfaced in
        ``ownership.peer`` as a ``PeerOwnedPath`` with
        ``claim_source="agent"``."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        agent = _agent_dir(repo, "aid-recent")
        (agent / "em-session-id.txt").write_text("mine")
        recent_ts = datetime.now(timezone.utc).timestamp() - 60
        _agent_claim(agent, "docs/research/in-flight.md", ts=recent_ts)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert "docs/research/in-flight.md" not in offer["safe_paths"]
        excluded_entries = [
            e for e in offer["excluded"] if e["path"] == "docs/research/in-flight.md"
        ]
        assert len(excluded_entries) == 1
        assert "aid-recent" in excluded_entries[0]["reason"]
        peer_entries = [
            p
            for p in offer["ownership"]["peer"]
            if p["path"] == "docs/research/in-flight.md"
        ]
        assert peer_entries == [
            {
                "path": "docs/research/in-flight.md",
                "owner": "aid-recent",
                "liveness": "live",
                "claim_source": "agent",
            }
        ]

    def test_a_stale_agent_claim_ages_out_and_stays_in_mine(self, tmp_path):
        """The anti-scope guard (moved/rewritten for C5, docs/plans/2026-08-
        27-safe-commit-offer-excludes-a-live-agent.md; previously named
        ``test_a_dead_agents_orphaned_claim_stays_in_mine`` under C3's now-
        retired liveness check): an agent dir whose dispatch already ended
        -- the agent process itself exited without releasing -- still needs
        its claim swept up by SOMEONE, or the path is orphaned forever (no
        retention/reaper for claimant dirs -- see ``claim_index``'s own
        docstring, INDEX PERSISTENCE). ``aid-stale`` here touched the path
        just past ``liveness._ABANDONMENT_WINDOW_SEC`` ago -- old enough to
        read as abandoned. Withholding every agent-owned claim regardless of
        recency would regress this: a finished agent's write would never
        again be offered to anyone as committable."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        agent = _agent_dir(repo, "aid-stale")
        (agent / "em-session-id.txt").write_text("mine")
        from coordinator_core.session import liveness

        stale_ts = (
            datetime.now(timezone.utc).timestamp()
            - liveness._ABANDONMENT_WINDOW_SEC
            - 60
        )
        _agent_claim(agent, "docs/research/orphaned.md", ts=stale_ts)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert "docs/research/orphaned.md" in offer["safe_paths"]
        assert [
            e for e in offer["excluded"] if e["path"] == "docs/research/orphaned.md"
        ] == []
        assert [
            p
            for p in offer["ownership"]["peer"]
            if p["path"] == "docs/research/orphaned.md"
        ] == []

    def test_an_agent_claim_with_no_parseable_timestamp_stays_in_mine(
        self, tmp_path
    ):
        """BIAS pin (C5): a legacy claim line with no parseable timestamp is
        absent from ``edit_ts`` entirely (``_IndexState``'s own contract),
        and the docstring's deliberate default is to resolve every unknown
        toward INCLUDING the path in ``safe_paths`` -- not toward treating an
        unresolvable timestamp as still in-flight. ``timestamp=0.0`` is the
        documented "unknown time" sentinel (`claim_index.rebuild`'s own
        AC21 comment) that never enters ``edit_ts``."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        agent = _agent_dir(repo, "aid-unknown-ts")
        (agent / "em-session-id.txt").write_text("mine")
        _agent_claim(agent, "docs/research/no-ts.md", ts=0.0)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert "docs/research/no-ts.md" in offer["safe_paths"]
        assert [
            e for e in offer["excluded"] if e["path"] == "docs/research/no-ts.md"
        ] == []

    def test_an_unclaimed_dirty_file_is_not_this_sessions_to_commit(self, tmp_path):
        """A dirty file with no claim ANYWHERE is not this session's, and the
        answer says so by omission -- it is absent from every bucket, not
        reported as an orphan.

        History, because this assertion has been inverted twice and the
        reasons are not interchangeable. Originally an uncontested dirty file
        with no `touched.txt` record joined `safe_paths` outright, by
        `compute_scope`'s Step-2 mtime fallback: recent mtime read as "I
        probably touched this and the hook missed it". DR-246 (2026-07-31)
        narrowed that to route such a candidate to `orphans` -- declined
        adoption, but still SEEN and reported. The 2026-08-21 rebuild removed
        the worktree read entirely, so there is no longer a surface on which
        this file is seen at all: the claim ledger decides, and the ledger has
        nothing to say about it. Same verdict as DR-246 (not mine), reached
        without a git spawn, and now unable to drift back -- there is no mtime
        heuristic left to relax.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["safe_paths"] == []
        assert offer["ownership"]["mine"] == []
        assert [e for e in offer["excluded"] if e["path"] == "orphan.py"] == []

    def test_orphans_is_always_empty_and_that_is_the_contract(self, tmp_path):
        """`orphans` is empty on EVERY call, including one with a dirty
        unclaimed file sitting right there -- the shape that used to populate
        it. Pinned as its own test so the emptiness reads as the contract it
        is, rather than as a degradation a reader goes hunting for the cause
        of. Repopulating it means re-adding a worktree read to this answer,
        which is the trade `compute_offer`'s negative spec forbids.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "unclaimed_by_anyone.py").write_text("x")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["indeterminate"] is False
        assert offer["orphans"] == []

    def test_empty_scope_is_a_valid_result(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert offer == {
            "session_id": "mine",
            "safe_paths": [],
            "excluded": [],
            "orphans": [],
            "indeterminate": False,
            # C5 (2026-08-05 in-process-writers-declare-their-writes): the
            # four-bucket ownership readout is ADDITIVE -- every key above is
            # unchanged. An empty tree still answers the ownership question,
            # and answers it emptily rather than omitting it: a consumer must
            # never have to distinguish "no ownership data" from "nothing
            # owned".
            "ownership": {
                "mine": [],
                "peer": [],
                "unattributed": [],
                "degraded": False,
            },
        }

    def test_required_session_id(self, tmp_path):
        with pytest.raises(ValueError):
            safe_commit_offer.compute_offer("", cwd=str(tmp_path))

    def test_orphan_skipped_via_unreadable_peer_claim_does_not_resurface(
        self, tmp_path, monkeypatch
    ):
        """Staff-eng F1 (2026-08-03) — the property this pins had NO test
        anywhere in the repo (confirmed by grep during that review):
        ``compute_offer``'s ``orphans`` is ``result.orphans`` MINUS
        ``skipped_paths``, so a path withheld for some OTHER reason (here:
        a peer's ``touched.txt`` is unreadable, so the candidate is
        skipped as "owned by session unknown" rather than genuinely
        unclaimed) can never resurface as an adoptable orphan.

        Built from the exact documented shape (this module's own
        docstring, "orphans" paragraph, and the F1 review comment on
        ``compute_offer``): a candidate withheld via
        ``unreadable_other_sessions`` lands in BOTH ``result.skipped``
        (owner "unknown (claims unreadable: ...)") and ``result.orphans``
        (Step 5: absent from ``my_scope``, and not actually claimed by
        anyone) — this is the same shape
        ``test_unreadable_peer_touched_dedupes_to_one_excluded_entry``
        above exercises for the `excluded` field; this test asserts the
        same fixture against `orphans` instead.

        Note (entanglement, honestly disclosed): in the CURRENT
        implementation this withhold arm also always sets
        ``result.indeterminate`` True (see ``ScopeResult.indeterminate``'s
        docstring — its only two triggers are
        ``unreadable_other_sessions`` and ``agent_race_paths``, and both
        are exactly the arms that can produce a skipped/orphans overlap in
        the first place), so this fixture cannot isolate the F1 subtraction
        from the separate R1 indeterminate-gate below
        (`test_indeterminate_call_withholds_orphans_outright`) — both fire
        together here. It remains a genuine regression witness: a refactor
        that dropped EITHER the F1 subtraction or the R1 gate would surface
        ``shared.py`` in ``offer["orphans"]`` and fail this test.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")

        other_touched = Path(core.session_dir("other", cwd=str(repo))) / "touched.txt"
        other_touched.write_text("shared.py\n")
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == other_touched:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert "shared.py" not in offer["orphans"]

    def test_unreadable_claim_set_makes_the_whole_answer_indeterminate(
        self, tmp_path, monkeypatch
    ):
        """A claim set this call could not READ makes the whole answer
        indeterminate -- not merely the path that could not be read. The walk
        behind `compute_offer` is ONE index rebuild, so an unreadable claimant
        means BOTH buckets may be short, and the caller must be told that
        rather than handed a partial answer as the answer.

        Post-2026-08-21 this also pins the `orphans` contract from the other
        side: it is empty here, as it is on EVERY call (see `compute_offer`'s
        own contract), so a reader cannot mistake the empty list for "the
        degradation emptied it" and go looking for the gate that did.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("other", "shared.py", cwd=str(repo))

        # C7b: `_read_lines_discard_torn_tail` was deleted with the legacy
        # line-dialect reader. `_read_stream_claims` is its replacement at
        # the same seam and keeps the same `(value, complete)` convention --
        # the blinding here is still "this one claimant's record cannot be
        # read", only the record's name and the value's shape changed.
        other_touched = os.path.join(
            core.session_dir("other", cwd=str(repo)), scope._TOUCH_RECORD_FILENAME
        )
        real_reader = claim_index._read_stream_claims

        def _unreadable(path):
            if os.path.normcase(str(path)) == os.path.normcase(other_touched):
                return {}, False
            return real_reader(path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["indeterminate"] is True
        assert offer["ownership"]["degraded"] is True
        # AC7: a call that could not finish its walk must not print an owner it
        # cannot stand behind, so `peer` empties outright.
        assert offer["ownership"]["peer"] == []
        assert offer["orphans"] == []

    def test_read_only_no_git_or_touched_mutation(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))
        touched_file = (
            Path(core.session_dir("mine", cwd=str(repo))) / scope._TOUCH_RECORD_FILENAME
        )
        before = touched_file.read_bytes()
        status_before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert touched_file.read_bytes() == before
        status_after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert status_before == status_after

    def test_read_only_no_agent_touched_mutation(self, tmp_path):
        """Same read-only guarantee as `test_read_only_no_git_or_touched_mutation`,
        extended to the SUB-AGENT fan-out's ``.agents/<aid>/touched.txt`` this
        module consumes via `_resolve_agent_touched_candidates` /
        `claims.my_agent_touched`.

        Negative-spec (touched-txt-release-is-append-only-correction, 2026-08-03
        cross-repo ruling — DoE-claude declared authoritative over the earlier,
        superseded `touched-txt-prunable-not-append-only`): `touched.txt` is an
        APPEND-ONLY `T`/`R` event log resolved last-event-wins, never a mutable
        set a reader may delete lines from. This module never writes to ANY
        `touched.txt` (session- or agent-keyed) at all — a future "receiver-side
        fix" that starts pruning/rewriting an agent's touched.txt from this
        read-only compute path would both violate that append-only contract and
        regress this pin."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        agent = _agent_dir(repo, "aid-mine")
        (agent / "em-session-id.txt").write_text("mine")
        agent_touched_file = _agent_claim(agent, "coordinator/agent_file.py")
        before = agent_touched_file.read_text()

        safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert agent_touched_file.read_text() == before


# ---------------------------------------------------------------------------
# (b) exact vs broadened — the 2026-07-31 finding
# ---------------------------------------------------------------------------


class TestExactModeOnly:
    def test_sibling_em_agent_fan_out_never_included(self, tmp_path):
        """A dispatched sub-agent belonging to a DIFFERENT EM session must
        never be unioned into `mine`'s candidates — this is exactly what
        "broadened" mode would do wrong (identical union for every session)
        and "exact" mode must not."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("sibling-em", cwd=str(repo))

        my_agent = _agent_dir(repo, "aid-mine")
        (my_agent / "em-session-id.txt").write_text("mine")
        _agent_claim(my_agent, "coordinator/mine_file.py")

        sibling_agent = _agent_dir(repo, "aid-sibling")
        (sibling_agent / "em-session-id.txt").write_text("sibling-em")
        _agent_claim(sibling_agent, "coordinator/sibling_file.py")

        candidates = safe_commit_offer._resolve_agent_touched_candidates(
            "mine", cwd=str(repo)
        )
        assert "coordinator/mine_file.py" in candidates
        assert "coordinator/sibling_file.py" not in candidates

    def test_broadened_mode_would_have_admitted_the_sibling_file(self, tmp_path):
        """Direct proof of the 2026-07-31 finding, not just an absence check:
        calling `my_agent_touched` with `"broadened"` on the SAME fixture as
        the test above returns the sibling EM's file too — i.e. this module
        deliberately does NOT use the mode that would leak it in. Kept as a
        permanent regression witness so a future edit that reverts
        `"exact"` -> `"broadened"` in `_resolve_agent_touched_candidates`
        fails this test, not just the one above."""
        from coordinator_core.session import liveness
        from coordinator_core.session.claims import my_agent_touched

        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("sibling-em", cwd=str(repo))

        my_agent = _agent_dir(repo, "aid-mine")
        (my_agent / "em-session-id.txt").write_text("mine")
        _agent_claim(my_agent, "coordinator/mine_file.py")

        sibling_agent = _agent_dir(repo, "aid-sibling")
        (sibling_agent / "em-session-id.txt").write_text("sibling-em")
        _agent_claim(sibling_agent, "coordinator/sibling_file.py")

        # live_session_ids() gates on recent meta.json activity; both
        # sessions were just core.init()'d so both are live -- this is what
        # makes "mine"'s broadened candidate set widen to include
        # "sibling-em" in the first place.
        assert set(liveness.live_session_ids(cwd=str(repo))) >= {"mine", "sibling-em"}

        broadened = my_agent_touched("mine", "broadened", cwd=str(repo))
        exact = my_agent_touched("mine", "exact", cwd=str(repo))

        assert "coordinator/sibling_file.py" in broadened
        assert "coordinator/sibling_file.py" not in exact

    def test_journal_form_agent_touched_yields_paths_and_honours_release(self, tmp_path):
        """End-to-end pin for the 2026-08-04 example-market-data-repo-em report:
        an agent `touched.txt` in `T`/`R` journal form must reach this
        module's candidate list as PATHS. Carried raw, every entry is a
        `'T <ISO> <path>'` string that matches no file, so this leg of
        `safe_paths` contributes nothing and a file the session's own
        dispatched agent wrote is refused as `unclaimed`. `R` events must
        drop their path — invisible to this leg while the lines were raw."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))

        agent = _agent_dir(repo, "aid-mine")
        (agent / "em-session-id.txt").write_text("mine")
        _agent_claim(
            agent,
            "T 2026-08-03T23:18:56.014950Z coordinator/kept.py",
            "T 2026-08-03T23:18:57.000000Z coordinator/released.py",
            "R 2026-08-03T23:20:00.000000Z coordinator/released.py",
        )

        candidates = safe_commit_offer._resolve_agent_touched_candidates(
            "mine", cwd=str(repo)
        )
        assert candidates == ["coordinator/kept.py"]
        assert not any(c.startswith("T ") or c.startswith("R ") for c in candidates)

    def test_mtime_alone_never_makes_a_file_this_sessions(self, tmp_path):
        """The paper trail for a hazard that has now been closed twice, kept
        because it was a real incident shape and a future reader deserves to
        know it was deliberately revisited rather than forgotten.

        A dirty file with NO `touched.txt` record anywhere -- a crashed peer,
        a pruned record, a session that never ran the touch hook -- was
        indistinguishable to `compute_scope`'s Step-2 mtime fallback from "a
        file I touched but the hook missed recording", and joined MY
        `safe_paths` if its mtime post-dated my `started_at`. DR-246
        (2026-07-31) narrowed Step 4 so it was declined instead. The
        2026-08-21 rebuild took `compute_offer` off `compute_scope` entirely,
        so the fallback is not consulted here at all.

        NOTE for anyone chasing this through: Step 2 still EXISTS in
        `session.scope`, and `coordinator/bin/coordinator-safe-commit.py`
        still reaches it. This test pins that THIS module's answer does not,
        which is a narrower claim than "the fallback is gone".
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "nobodys_file.py").write_text("z")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert "nobodys_file.py" not in offer["safe_paths"]



# ---------------------------------------------------------------------------
# (c) path normalization
# ---------------------------------------------------------------------------


class TestNormalizeAgentTouchedEntry:
    # POST-C2: `.agents/<aid>/touched.txt` entries are already repo-relative
    # (the writer emits clean repo-relative paths directly, no `../` prefix)
    # — see safe_commit_offer.py module docstring's "Path-format fix,
    # SUPERSEDED" note. These entries pass through unchanged (only sep/`.`/
    # `..` segment normalization applies), never joined onto a plugin-dir
    # prefix.
    @pytest.mark.parametrize(
        "entry,expected",
        [
            ("coordinator/agents/code-reviewer.md", "coordinator/agents/code-reviewer.md"),
            ("state/foo.md", "state/foo.md"),
            ("coordinator_core/ops/session/safe_commit_offer.py", "coordinator_core/ops/session/safe_commit_offer.py"),
        ],
    )
    def test_repo_relative_entry_passes_through(self, entry, expected):
        assert safe_commit_offer._normalize_agent_touched_entry(entry) == expected

    def test_directory_entry_keeps_trailing_slash(self):
        result = safe_commit_offer._normalize_agent_touched_entry("coordinator/skills/")
        assert result == "coordinator/skills/"

    @pytest.mark.parametrize(
        "entry",
        [
            "../coordinator_core/foo.py",
            "../../../../../private/tmp/claude-501/scratch/x.py",
            "..",
            ".",
        ],
    )
    def test_cross_repo_or_out_of_repo_entry_dropped(self, entry):
        assert safe_commit_offer._normalize_agent_touched_entry(entry) is None

    def test_empty_entry_dropped(self):
        assert safe_commit_offer._normalize_agent_touched_entry("") is None

    @pytest.mark.parametrize(
        "entry",
        [
            "/etc/passwd",
            "\\Users\\someone\\.ssh\\id_rsa",
            "C:" + "\\some\\drive\\letter\\path",  # abs-path-ok: synthetic drive-letter shape, not a real host path
        ],
    )
    def test_absolute_entry_dropped(self, entry):
        # Review: code-reviewer (Finding 3) — posixpath.join("coordinator",
        # "/etc/passwd") discards the plugin-dir prefix under POSIX join
        # semantics when the second argument is absolute, so an absolute
        # entry previously survived normalization unchanged. Covers
        # POSIX-absolute, backslash-absolute, and Windows drive-letter shapes.
        assert safe_commit_offer._normalize_agent_touched_entry(entry) is None


# ---------------------------------------------------------------------------
# (d) directory-entry expansion
# ---------------------------------------------------------------------------


class TestDirtyFilesUnder:
    def test_finds_dirty_files_under_directory_only(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        (repo / "sub" / "a.py").write_text("a")
        (repo / "outside.py").write_text("o")
        found = safe_commit_offer._dirty_files_under("sub", cwd=str(repo))
        assert found == ["sub/a.py"]

    def test_no_dirty_files_returns_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        assert safe_commit_offer._dirty_files_under("sub", cwd=str(repo)) == []


class TestDirtyFilesUnderBatch:
    """C16 — the batched sibling of `_dirty_files_under`. § Anti-scope 25:
    a directory entry with nothing dirty under it must come back as a
    PRESENT key with an empty list, never be silently dropped."""

    def test_matches_per_entry_results_of_the_single_path_form(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        (repo / "sub" / "a.py").write_text("a")
        (repo / "other").mkdir()
        (repo / "other" / "b.py").write_text("b")
        (repo / "outside.py").write_text("o")

        batched = safe_commit_offer._dirty_files_under_batch(
            ["sub/", "other/"], cwd=str(repo)
        )

        assert batched["sub/"] == safe_commit_offer._dirty_files_under("sub", cwd=str(repo))
        assert batched["other/"] == safe_commit_offer._dirty_files_under("other", cwd=str(repo))

    def test_clean_directory_is_a_present_key_with_empty_list_not_absent(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        (repo / "clean").mkdir()
        (repo / "sub" / "a.py").write_text("a")

        batched = safe_commit_offer._dirty_files_under_batch(
            ["sub/", "clean/"], cwd=str(repo)
        )

        assert "clean/" in batched
        assert batched["clean/"] == []
        assert batched["sub/"] == ["sub/a.py"]

    def test_empty_input_returns_empty_ordered_dict(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert safe_commit_offer._dirty_files_under_batch([], cwd=str(repo)) == {}

    def test_duplicate_dir_entries_collapse_to_one_key(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        (repo / "sub" / "a.py").write_text("a")

        batched = safe_commit_offer._dirty_files_under_batch(
            ["sub/", "sub/"], cwd=str(repo)
        )

        assert list(batched.keys()) == ["sub/"]
        assert batched["sub/"] == ["sub/a.py"]

    def test_git_failure_fails_closed_every_value_empty(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        (repo / "sub").mkdir()
        (repo / "sub" / "a.py").write_text("a")

        def _boom(*args, **kwargs):
            raise OSError("no git")

        monkeypatch.setattr(subprocess, "run", _boom)
        batched = safe_commit_offer._dirty_files_under_batch(["sub/"], cwd=str(repo))
        assert batched == {"sub/": []}


# ---------------------------------------------------------------------------
# (e) commit_session_offer — the mutating half, no confirmation
# ---------------------------------------------------------------------------


class TestAutoCommitSession:
    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_no_confirmation_step_lands_a_real_commit(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is True
        assert report["groups"][0]["sha"]
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert "a.py" not in log  # commit landed, message is the mechanical subject
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert status == ""  # nothing left dirty

    def test_commit_group_lands_a_real_commit_through_the_pipeline(self, tmp_path):
        """End-to-end guard that `_commit_group` actually commits.

        History: this began as a 2026-08-07 regression guard pinning that
        `ceremony.scoped_git_commit`'s handler was a plain sync `def`, because
        `_commit_group` had awaited the dict it returns and killed every
        `safe-commit-offer` invocation (cross-repo/inbox/2026-08-07-project-
        rag-em-safe-commit-offer-await-dict-typeerror.md). It was then marked
        `designed_red` against that op's 2026-08-21 budget suspension.

        Both pins are obsolete: the op was DELETED 2026-08-23 under DR-344's
        kill bar, so `get_op_handler` returns None forever and the preamble
        asserting otherwise could never pass again -- it failed before the
        behavioural assertions below ever ran, which is how a live guard came
        to read as permanently-red bookkeeping. `_commit_group` now routes
        through `coordinator_core.git.commit.commit_paths` (C4 repoint,
        docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-
        pipeline-can-go.md).

        The assertions below are the ones that carried the value, and they are
        deliberately NOT mocked: the sibling tests stub `commit_paths` to
        prove the result mapping, which cannot show that a commit lands.
        This one drives a real repo and reads the sha back."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        result = asyncio.run(
            safe_commit_offer._commit_group(
                str(repo), {"paths": ["a.py"], "message": "regression guard"}, "mine"
            )
        )
        assert result["committed"] is True
        assert result["error"] is None
        assert result["commit_failed"] is False

        # The sha is the part a mock cannot fake: read it back off the repo and
        # confirm the commit it names carries the path we handed in, and only it.
        assert result["sha"]
        landed = subprocess.run(
            ["git", "show", "--name-only", "--format=", result["sha"]],
            cwd=repo,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        ).stdout.split()
        assert landed == ["a.py"], landed
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert status == ""  # nothing left dirty

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_default_grouping_subject_stays_bounded_body_carries_the_list(self, tmp_path):
        """Regression guard for the enumerated-filenames-in-subject shape:
        the subject must stay short/bounded regardless of file count, and the
        full path list must land in the commit BODY, not the subject. The
        bound is the point and it is independent of framing — see
        TestDefaultGroupsFraming for what the body may and may not claim now
        that ``invoker`` is deleted (2026-08-27)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        paths = []
        for i in range(12):
            p = "sub/file_%02d.py" % i
            (repo / "sub").mkdir(exist_ok=True)
            (repo / p).write_text(str(i))
            scope.touch("mine", p, cwd=str(repo))
            paths.append(p)

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        subject = report["groups"][0]["message"]
        assert len(subject) < 100
        assert "file_00.py" not in subject  # no enumerated filenames in the subject

        full_message = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        for p in paths:
            assert p in full_message  # every rescued path IS in the body
        # STALE ASSERTION CORRECTED 2026-08-29. This asserted the body carried
        # "safety net" framing. That framing was deliberately retired -- the
        # module docstring records it as "describing a shape nothing
        # implements", and `TestDefaultGroupsFraming` below now asserts the
        # opposite property (the body says HOW it was grouped and explicitly
        # disclaims WHY). The test was left asserting the retired wording, so
        # it has been failing at HEAD independently of any change here; it is
        # repointed at the framing that actually ships rather than deleted,
        # because "the body carries framing at all" is still worth guarding.
        assert "mechanically grouped" in full_message.lower()

    def test_nothing_to_commit_is_a_valid_noop(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        assert report["groups"] == []
        assert report["excluded"] == []

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_explicit_groups_never_widen_past_safe_paths(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "a.py").write_text("a")
        (repo / "peer.py").write_text("p")
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["a.py", "peer.py"], "message": "smuggled group"}],
        )

        assert len(report["groups"]) == 1
        assert report["groups"][0]["paths"] == ["a.py"]  # peer.py silently dropped
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert "peer.py" in status  # still dirty, never committed

    def test_group_with_only_excluded_paths_is_dropped_entirely(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["peer.py"], "message": "all-excluded group"}],
        )
        assert report["groups"] == []

    def test_all_dropped_group_is_recorded_in_dropped_groups(self, tmp_path):
        # Handoff item 1 (2026-08-03, touched-path-bookkeeping) -- the
        # all-excluded group above vanished from `groups`, `failed_groups`,
        # and `excluded` (`compute_offer`-derived, not group-derived) all at
        # once, with no trace anywhere in the report. `dropped_groups` is
        # that trace.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["peer.py"], "message": "all-excluded group"}],
        )
        assert report["dropped_groups"] == [
            {"message": "all-excluded group", "named": 1, "matched": 0}
        ]

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_partially_dropped_group_is_also_recorded(self, tmp_path):
        # A group losing 4 of 5 paths is the same silence in miniature --
        # must be recorded even though the group itself still commits.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "a.py").write_text("a")
        (repo / "peer.py").write_text("p")
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["a.py", "peer.py"], "message": "partial group"}],
        )
        assert report["dropped_groups"] == [
            {"message": "partial group", "named": 2, "matched": 1}
        ]
        assert report["groups"][0]["paths"] == ["a.py"]

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_default_groups_never_populate_dropped_groups(self, tmp_path):
        # `groups=None` (the unattended-trigger fallback) is computed FROM
        # `safe_paths` itself, so it can never lose a path to the filter --
        # `dropped_groups` must stay empty for that path.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        assert report["dropped_groups"] == []

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_message_flag_produces_one_group(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        (repo / "b.py").write_text("b")
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("mine", "b.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine", cwd=str(repo), groups=[{"paths": ["a.py", "b.py"], "message": "one subject"}]
        )
        assert len(report["groups"]) == 1
        assert report["groups"][0]["message"] == "one subject"
        assert set(report["groups"][0]["paths"]) == {"a.py", "b.py"}

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_explicit_group_prose_reaches_the_commit_body(self, tmp_path):
        # Review: code-reviewer (Finding 4) — the explicit-`groups` branch of
        # commit_session_offer_async previously rebuilt each group without
        # carrying `g.get("prose")` through, so caller-supplied body text was
        # silently dropped and only the mechanical `_default_groups` fallback
        # ever produced a non-empty commit body.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        safe_commit_offer.commit_session_offer(
            "mine",
            cwd=str(repo),
            groups=[
                {
                    "paths": ["a.py"],
                    "message": "curated subject",
                    "prose": "curated body text with real judgment behind it",
                }
            ],
        )
        full_message = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert "curated body text with real judgment behind it" in full_message

    def test_nothing_to_commit_noop_has_no_failed_groups(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        assert report["failed_groups"] == []

    def test_commit_failure_surfaces_via_failed_groups_not_swallowed(self, tmp_path, monkeypatch):
        """C4 repoint (docs/plans/2026-08-29-the-push-subsystem-leaves-and-
        then-the-pipeline-can-go.md): `_commit_group` now calls
        `coordinator_core.git.commit.commit_paths` directly, in-process --
        mocked here at `safe_commit_offer.commit_paths`. A `CommitRefused`
        must surface through `CommitOfferReport.failed_groups`, distinct
        from a landed commit (next test) -- constraint 2 of the original
        DR-344 rewiring's brief, unchanged by this repoint: a false
        `committed: True` is worse than the prior dead stub."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        captured_calls = []

        def fake_commit_paths(worktree_root, paths, message, **kwargs):
            captured_calls.append({"worktree_root": worktree_root, "paths": paths, "message": message, **kwargs})
            raise CommitRefused("dirty-tree gate: unattributable paths: peer.py")

        monkeypatch.setattr(safe_commit_offer, "commit_paths", fake_commit_paths)

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is False
        assert report["groups"][0]["commit_failed"] is True
        assert report["groups"][0]["error"]
        assert report["failed_groups"] == [report["groups"][0]]
        # constraint 3 -- exactly the group's own paths were staged, never a
        # caller-widened or auto-detected pathspec.
        assert captured_calls[0]["paths"] == ["a.py"]

    def test_commit_group_maps_a_landed_pipeline_result(self, tmp_path, monkeypatch):
        """A successful `commit_paths()` call maps to `committed: True` with
        the landed sha populated -- constraint 1 (populate `GroupResult`
        verbatim from the outcome, never inventing a shape)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        monkeypatch.setattr(
            safe_commit_offer,
            "commit_paths",
            lambda worktree_root, paths, message, **kwargs: CommitOutcome(
                sha="deadbeef", staged_preferred=(), worktree_over_staged=()
            ),
        )

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is True
        assert report["groups"][0]["sha"] == "deadbeef"
        assert report["groups"][0]["commit_failed"] is False
        assert report["groups"][0]["error"] is None
        assert report["failed_groups"] == []

    def test_commit_failed_group_never_carries_a_reason(self, tmp_path, monkeypatch):
        # `GroupResult.reason`'s own docstring: "None on a landed commit or
        # a genuine `commit_failed`" -- `_render_report`'s two branches
        # (the "NOT committed" line vs the quiet benign-no-op line) depend
        # on that split. Under the `commit_paths()` composition (C4 repoint)
        # a refusal never carries a `reason` of its own -- `_commit_group`'s
        # refusal branch hard-codes `reason: None`, so this pins the
        # suppression that keeps `_render_report` honest.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def _refuse(worktree_root, paths, message, **kwargs):
            raise CommitRefused("nothing to commit")

        monkeypatch.setattr(safe_commit_offer, "commit_paths", _refuse)

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert report["groups"][0]["reason"] is None
        rendered = safe_commit_offer._render_report(report)
        assert "NOT committed" in rendered


# ---------------------------------------------------------------------------
# (f) The op handler
# ---------------------------------------------------------------------------
#
# Ported 2026-08-27 from `TestMain`, which drove `safe_commit_offer.main(argv)`
# and asserted on exit codes plus captured stdout. Both are gone with the CLI:
# the op returns its whole report on the wire, so an outcome that used to be an
# exit code is now a field. The mapping, kept explicit because those exit codes
# are cited by name in this module's own docstrings and in DoE-claude's:
#   exit 0 -> ran; `error` absent and `failed_groups` empty
#   exit 1 -> `error` == "session_id could not be resolved"
#   exit 2 -> `error` naming the violated precondition (usage)
#   exit 4 -> `failed_groups` non-empty
# The rendered operator text that used to be stdout is the `rendered` field,
# which is why these assertions read it rather than `capsys`.


class TestHandler:
    def test_dry_run_computes_without_committing(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        out = safe_commit_offer._handler(
            {"session_id": "mine", "cwd": str(repo), "dry_run": True}
        )
        assert "error" not in out
        assert out["dry_run"] is True
        assert out["safe_paths"] == ["a.py"]
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert "a.py" in status  # NOT committed

    def test_dry_run_names_dirty_paths_no_session_claims(self, tmp_path):
        """The shape doe-claude-em reported three times (2026-08-18, -21, -30):
        a session whose whole working set was written through Bash carries no
        claim, so the offer answered `safe_paths: 0, excluded: 0` over a tree
        with real modifications -- byte-identical to a clean tree.

        The bucket that names them existed since 2026-08-29 but only on the
        COMMIT path, i.e. only after the decision it informs was already taken.
        This asserts the inspection path names it too, and that naming is all
        it does: the path stays out of `safe_paths` (never adopted) and stays
        uncommitted.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "written-by-a-heredoc.py").write_text("x")

        out = safe_commit_offer._handler(
            {"session_id": "mine", "cwd": str(repo), "dry_run": True}
        )

        assert out["safe_paths"] == []
        assert out["excluded"] == []
        assert out["reconciliation"]["reconciled"] is True
        assert "written-by-a-heredoc.py" in out["reconciliation"]["unclaimed"]
        assert "written-by-a-heredoc.py" in out["rendered"]
        assert "unclaimed-and-dirty: 1" in out["rendered"]

    def test_dry_run_does_not_call_a_claimed_path_unclaimed(self, tmp_path):
        """The subtraction that keeps the bucket honest: a dirty path this
        session DOES claim is its own work awaiting commit, not an adoption
        candidate. Folding the two together is what would make the enumeration
        unsafe to hand `--include-orphans`.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "claimed.py").write_text("a")
        (repo / "unclaimed.py").write_text("b")
        scope.touch("mine", "claimed.py", cwd=str(repo))

        out = safe_commit_offer._handler(
            {"session_id": "mine", "cwd": str(repo), "dry_run": True}
        )

        assert out["safe_paths"] == ["claimed.py"]
        assert out["reconciliation"]["unclaimed"] == ["unclaimed.py"]

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_message_param_commits(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        out = safe_commit_offer._handler(
            {"session_id": "mine", "cwd": str(repo), "message": "hand-authored subject"}
        )
        assert "error" not in out
        assert out["failed_groups"] == []
        assert "hand-authored subject" in out["rendered"]
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert status == ""

    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_groups_param(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        out = safe_commit_offer._handler(
            {
                "session_id": "mine",
                "cwd": str(repo),
                "groups": [{"paths": ["a.py"], "message": "from params"}],
            }
        )
        assert "error" not in out
        assert "from params" in out["rendered"]

    def test_message_and_groups_mutually_exclusive(self, tmp_path):
        out = safe_commit_offer._handler({"message": "x", "groups": []})
        assert out["error"] == "params.message and params.groups are mutually exclusive"

    def test_groups_must_be_a_list(self, tmp_path):
        out = safe_commit_offer._handler({"groups": "not-a-list"})
        assert "params.groups must be a list" in out["error"]

    def test_unresolvable_session_returns_error_envelope(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        out = safe_commit_offer._handler({"cwd": str(repo)})
        assert "caller identity could not be established" in out["error"]
        assert out["session_id"] == ""

    def test_ambient_environment_identity_is_refused_not_used(self, tmp_path, monkeypatch):
        """The live 2026-08-27 defect, reported by doe-claude-em: dialled through
        coordinator-invoke.exe from three different cwd values, the op returned
        the ENGINE OWNER's session id every time, because the exe does not send
        `_session_id` and `resolve_session_id(cwd)` therefore fell through to
        tiers 1-3 — this process's environment. A non-dry run would have
        committed the engine owner's paths under the calling ceremony's claim.

        `cwd` was never the fix: `resolve_session_id`'s own docstring says
        "cwd is retained for API compatibility with existing callers even
        though tiers 0-3 do not use it" (tier 4 removed, KS-4).

        So: an ambient env identity, with nothing carried by the caller, must
        REFUSE. This asserts the refusal directly rather than asserting the
        returned id, because the bug's signature was a plausible-looking id that
        simply belonged to the wrong session.
        """
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "11111111-2222-3333-4444-555555555555")
        core.init("11111111-2222-3333-4444-555555555555", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("11111111-2222-3333-4444-555555555555", "a.py", cwd=str(repo))

        out = safe_commit_offer._handler({"cwd": str(repo)})

        assert "caller identity could not be established" in out["error"], (
            "the ambient environment answered for a session that never called"
        )
        assert out["session_id"] == ""

    def test_carried_identity_is_accepted(self, tmp_path):
        """The other half: identity the caller DID carry (tier 0, bound by
        `warm.entry_seam.per_request_state` from the wire's `_session_id`) is
        the one ambient-free source, and must still work — a refusal that also
        rejected the correct path would just move the breakage.
        """
        repo = _make_repo(tmp_path)
        sid = "1420e948-69f0-4e01-a44e-1853891f1795"
        core.init(sid, cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch(sid, "a.py", cwd=str(repo))

        with core.session_identity_override(sid):
            out = safe_commit_offer._handler({"cwd": str(repo), "dry_run": True})

        assert "error" not in out
        assert out["session_id"] == sid
        assert out["safe_paths"] == ["a.py"]

    def test_benign_noop_reports_no_failure(self, tmp_path):
        # Regression guard: the ordinary already-committed no-op must NEVER
        # be conflated with a genuine `commit_failed` group -- `git commit`
        # itself exits 1 on an empty commit set, so a version that cried wolf
        # here would fire on every ordinary ceremony run. Post-CLI the
        # wolf-cry surface is `failed_groups`/`error`, not an exit code.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        out = safe_commit_offer._handler({"session_id": "mine", "cwd": str(repo)})
        assert "error" not in out
        assert out["failed_groups"] == []

    def test_genuine_commit_failure_populates_failed_groups_and_logs_diagnostic(
        self, tmp_path, monkeypatch
    ):
        # Review: code-reviewer (Finding 1) — `failed_groups` was computed and
        # tested but never surfaced anywhere a real caller read. The
        # diagnostics write is what closes that gap, and it outlives the call,
        # which the return value does not.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def _refuse(worktree_root, paths, message, **kwargs):
            raise CommitRefused("dirty-tree gate: unattributable paths: peer.py")

        monkeypatch.setattr(safe_commit_offer, "commit_paths", _refuse)

        out = safe_commit_offer._handler({"session_id": "mine", "cwd": str(repo)})
        assert out["failed_groups"] != []

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        assert log_file.is_file()
        contents = log_file.read_text()
        assert "1 group(s) genuinely failed" in contents
        assert "unattributable paths: peer.py" in contents

    def test_withheld_contested_path_logs_diagnostic_and_is_not_a_failure(
        self, tmp_path
    ):
        # `_log_excluded_diagnostic` is the sink that outlives the call, so a
        # withheld path that never reaches it is a path nobody learns about.
        # Withholding a contested path is the correct outcome (DR-227), never a
        # failure -- `failed_groups` stays empty.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        out = safe_commit_offer._handler({"session_id": "mine", "cwd": str(repo)})
        assert out["failed_groups"] == []

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        assert log_file.is_file()
        contents = log_file.read_text()
        assert "withheld" in contents
        assert "shared.py" in contents
        assert "owned by session peer" in contents

    def test_withheld_preview_bound_enforced_on_written_log(self, tmp_path):
        # The bound is enforced on what is WRITTEN, not merely on a rendered
        # string: only `_EXCLUDED_LOG_PREVIEW_COUNT` path lines appear and the
        # remainder is NAMED via an "and N more" tail rather than dropped
        # silently.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        total = safe_commit_offer._EXCLUDED_LOG_PREVIEW_COUNT + 3
        paths = []
        for i in range(total):
            rel = "shared_%02d.py" % i
            (repo / rel).write_text(str(i))
            scope.touch("mine", rel, cwd=str(repo))
            scope.touch("peer", rel, cwd=str(repo))
            paths.append(rel)

        out = safe_commit_offer._handler({"session_id": "mine", "cwd": str(repo)})
        assert out["failed_groups"] == []

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        contents = log_file.read_text()
        assert "... and 3 more" in contents
        present = [rel for rel in paths if rel in contents]
        assert len(present) == safe_commit_offer._EXCLUDED_LOG_PREVIEW_COUNT

    def test_all_dropped_group_is_named_matched_and_logged_not_a_failure(self, tmp_path):
        # Handoff item 1 (2026-08-03, touched-path-bookkeeping) -- an
        # all-dropped caller-supplied group was previously silent: absent from
        # `groups`, `failed_groups`, AND `excluded` (which is
        # `compute_offer`-derived, not group-derived) all at once. This is the
        # regression guard for the fix: the rendered text, the diagnostics-log
        # sink, and `failed_groups` all reflect it, and it is never reported as
        # a commit failure.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        out = safe_commit_offer._handler(
            {
                "session_id": "mine",
                "cwd": str(repo),
                "groups": [{"paths": ["peer.py"], "message": "all-excluded group"}],
            }
        )

        assert out["failed_groups"] == []
        assert "all-excluded group — named 1 paths, 0 matched" in out["rendered"]
        assert "NOT committed" not in out["rendered"]

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        assert log_file.is_file()
        log_contents = log_file.read_text()
        assert "1 caller-supplied group(s) partially or fully dropped" in log_contents
        assert "all-excluded group — named 1 paths, 0 matched" in log_contents

    def test_dropped_groups_render_and_log_are_bounded(self, tmp_path):
        # Bounded-output guard: many dropped groups must still render (and
        # log) a bounded number of lines plus an "and N more group(s)" tail --
        # never one line per group unbounded, the same shape the 1938-entry
        # `excluded` incident this module already retired once for a different
        # field.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))

        total = safe_commit_offer._DROPPED_GROUPS_PREVIEW_COUNT + 3
        groups = []
        for i in range(total):
            rel = "peer_%02d.py" % i
            (repo / rel).write_text(str(i))
            scope.touch("other", rel, cwd=str(repo))
            groups.append({"paths": [rel], "message": "dropped group %02d" % i})

        out = safe_commit_offer._handler(
            {"session_id": "mine", "cwd": str(repo), "groups": groups}
        )
        rendered = out["rendered"]

        assert out["failed_groups"] == []
        assert "... and 3 more group(s)" in rendered
        shown = [g for g in groups if g["message"] in rendered]
        assert len(shown) == safe_commit_offer._DROPPED_GROUPS_PREVIEW_COUNT

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        log_contents = log_file.read_text()
        assert "... and 3 more group(s)" in log_contents

# ---------------------------------------------------------------------------
# (g) AC0 — post-C2 dialect: a peer sub-agent claim still shadows a candidate
# in compute_scope's Step 3b other_owner map after the writer stops emitting
# `../`-prefixed entries. Regression guard for the C0/C2 atomic pair: if
# `_normalize_agent_touched_entry` regresses to re-joining a plugin-dir
# prefix onto an already repo-relative entry, this peer claim silently
# vanishes and the candidate widens `my_scope` in the unsafe direction.
# ---------------------------------------------------------------------------


class TestPeerAgentClaimStillShadowsPostC2Dialect:
    def test_peer_dispatched_agent_claim_shadows_candidate_post_c2_dialect(
        self, tmp_path
    ):
        repo = _make_repo(tmp_path)
        # Both sessions are made LIVE by core.init() (fresh meta.json
        # activity) -- required per compute_scope's liveness gate (commit
        # 0e07aecd): a peer claim only folds into `other_owner` for a
        # session in `liveness.live_session_ids`. Without this, the peer
        # claim would be released as dead for a reason unrelated to this
        # test's own assertion, and the assertion would pass vacuously.
        core.init("em-owner", cwd=str(repo))
        core.init("bystander", cwd=str(repo))
        assert set(scope.liveness.live_session_ids(cwd=str(repo))) >= {
            "em-owner",
            "bystander",
        }

        agent_dir = _agent_dir(repo, "agent-post-c2")
        (agent_dir / "em-session-id.txt").write_text("em-owner\n", encoding="utf-8")
        # POST-C2 dialect: the fixed writer emits a clean repo-relative
        # entry directly -- no `../` prefix, no plugin-dir join needed by
        # the reader.
        _agent_claim(agent_dir, "peer_agent_owned.py")
        (repo / "peer_agent_owned.py").write_text("z")

        result = scope.compute_scope("bystander", cwd=str(repo))

        assert "peer_agent_owned.py" not in result.my_scope
        assert "peer_agent_owned.py" not in result.orphans
        assert ("peer_agent_owned.py", "em-owner") in result.skipped


# ---------------------------------------------------------------------------
# (h) Report counts — a landed commit's line must name what the commit CHANGED,
# labelled apart from the breadth of the pathspec it was handed. Live
# (example-cockpit-repo-em memo, 2026-08-05), a 1-file commit was reported as
# "14 file(s)" because the other 13 in-scope paths had nothing to commit; on a
# shared branch that reads as a sweep of a peer's work, settleable only by the
# `git show --stat` the report exists to save.
# ---------------------------------------------------------------------------


def _landed_commit(repo, filenames):
    """Commit `filenames` (created here) and return the new sha."""
    for name in filenames:
        (repo / name).write_text("x\n")
    subprocess.run(["git", "add", *filenames], cwd=repo, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "landed"], cwd=repo, check=True, **no_console_passthrough_kwargs())
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _group(**over):
    group = {
        "paths": ["a.py"],
        "message": "feat: a",
        "committed": False,
        "sha": None,
        "push_state": None,
        "error": None,
        "commit_failed": False,
        "reason": None,
    }
    group.update(over)
    return group


def _report(group):
    return {
        "session_id": "mine",
        "groups": [group],
        "excluded": [],
        "failed_groups": [],
    }


@pytest.fixture(autouse=True)
def _clear_normalize_latch(monkeypatch):
    """The normalize diagnostic latch is module-global and one-shot per
    process — reset it around every test in this file so neither a real
    `normalize_touch_path` fail-open earlier in the session nor a test that
    sets it deliberately leaks a DEGRADED INPUT line into a sibling's
    rendered report."""
    monkeypatch.setattr(scope, "_normalize_diag_fired", False)


class TestRenderReportCounts:
    def test_changed_count_and_scope_count_reported_separately(self, tmp_path, monkeypatch):
        """C5 (2026-08-15 composition-invocation-budgets) retired the
        per-commit `git show --name-only` spawn `_commit_changed_count` used
        to run; this pins the replacement contract — the report renders the
        in-scope pathspec breadth with NO subprocess spawned to compute it —
        rather than the retired "N changed (M in scope)" wording this test
        previously pinned."""
        repo = _make_repo(tmp_path)
        sha = _landed_commit(repo, ["one.py"])

        def _no_spawn(*args, **kwargs):
            raise AssertionError("no subprocess should be spawned by _render_report")

        monkeypatch.setattr(safe_commit_offer.subprocess, "run", _no_spawn)

        rendered = safe_commit_offer._render_report(
            _report(
                _group(
                    paths=["one.py"] + ["in_scope_%d.py" % i for i in range(13)],
                    committed=True,
                    sha=sha,
                    push_state="pushed",
                )
            ),
            str(repo),
        )

        assert "14 file(s) in scope" in rendered
        assert "changed (" not in rendered, (
            "the retired changed-count wording must not resurface"
        )

    def test_equal_counts_still_render_both_labelled(self, tmp_path, monkeypatch):
        """Equal named-vs-committed path counts still render the plain
        in-scope count sensibly, with no spawn attempted."""
        repo = _make_repo(tmp_path)
        sha = _landed_commit(repo, ["one.py", "two.py"])

        def _no_spawn(*args, **kwargs):
            raise AssertionError("no subprocess should be spawned by _render_report")

        monkeypatch.setattr(safe_commit_offer.subprocess, "run", _no_spawn)

        rendered = safe_commit_offer._render_report(
            _report(
                _group(
                    paths=["one.py", "two.py"],
                    committed=True,
                    sha=sha,
                    push_state="pushed",
                )
            ),
            str(repo),
        )

        assert "2 file(s) in scope" in rendered
        assert "changed (" not in rendered

    def test_undeterminable_change_count_degrades_to_labelled_scope_only(self, tmp_path):
        repo = _make_repo(tmp_path)

        rendered = safe_commit_offer._render_report(
            _report(
                _group(
                    paths=["one.py", "two.py"],
                    committed=True,
                    sha="0" * 40,
                    push_state="pushed",
                )
            ),
            str(repo),
        )

        assert "2 file(s) in scope" in rendered
        assert "changed (" not in rendered

    def test_no_worktree_root_does_not_probe_an_unknown_cwd(self, tmp_path):
        repo = _make_repo(tmp_path)
        sha = _landed_commit(repo, ["one.py"])

        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["one.py"], committed=True, sha=sha, push_state="pushed"))
        )

        assert "1 file(s) in scope" in rendered

    def test_already_committed_branch_labels_its_count_as_scope(self):
        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["a.py", "b.py"], reason="empty-commit-set"))
        )

        assert (
            "already committed — feat: a — 2 file(s) in scope, nothing new to "
            "commit (empty-commit-set)" in rendered
        )
        assert "changed" not in rendered, (
            "no change count may be attached to a commit that did not happen"
        )


class TestRenderReportDegradedScopeInput:
    def test_notice_absent_when_latch_clear(self, tmp_path):
        repo = _make_repo(tmp_path)
        sha = _landed_commit(repo, ["one.py"])

        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["one.py"], committed=True, sha=sha, push_state="pushed")),
            str(repo),
        )

        assert "DEGRADED INPUT" not in rendered

    def test_notice_leads_the_report_when_latch_set(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        sha = _landed_commit(repo, ["one.py"])
        monkeypatch.setattr(scope, "_normalize_diag_fired", True)

        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["one.py"], committed=True, sha=sha, push_state="pushed")),
            str(repo),
        )
        lines = rendered.splitlines()

        assert scope.normalize_diagnostic_fired() is True
        assert lines[0].startswith("DEGRADED INPUT")
        assert "touch record" in lines[0] and "may be incomplete" in lines[0]
        assert "committed" in lines[-1], (
            "the degraded-input notice leads; the verdict must still be last "
            "(coordinator/tests/test_safe_commit_offer_outcome_signal.py)"
        )

    def test_touching_a_path_outside_this_repo_produces_no_banner(self, tmp_path):
        """The calibration fix, end to end through the real writer.

        A session touching an absolute path outside its own repo — a sibling
        repo, a settings home, a scratch dir — is routine, and in this fleet
        it is the DOMINANT case. `git ls-files` exits 128 on it,
        `normalize_touch_path`'s relpath fallback handles it correctly, and
        nothing is degraded. Before `scope._ls_files_failure_is_benign`, that
        armed the latch and so put a false DEGRADED INPUT banner at the head
        of most safe-commit-offer reports — a false alarm in the top line of
        the exact report that same commit (eb1e8b5d76c8) was fixing for being
        falsely alarming.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        outside = tmp_path.parent / "outside-this-repo.md"
        outside.write_text("x")

        scope.touch("mine", str(outside), cwd=str(repo))
        sha = _landed_commit(repo, ["one.py"])
        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["one.py"], committed=True, sha=sha, push_state="ok")),
            str(repo),
        )

        assert scope.normalize_diagnostic_fired() is False
        assert "DEGRADED INPUT" not in rendered

    def test_operational_failure_during_touch_produces_the_banner(
        self, tmp_path, monkeypatch
    ):
        """The counterpart: with the benign case filtered out, a latch that IS
        set means an unclassified normalization failure, and the report that
        names the boundary must still say so."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        monkeypatch.setattr(scope, "_git_run", lambda args, cwd=None: None)

        # No monkeypatch.undo() here: `monkeypatch` is one function-scoped
        # instance shared with the autouse latch-reset fixture, so undoing it
        # would ALSO restore `_normalize_diag_fired` and erase the very latch
        # this test is asserting on. The patch is scoped to `scope._git_run`
        # and touches nothing the render path below uses.
        scope.touch("mine", str(repo / "in_repo.py"), cwd=str(repo))
        sha = _landed_commit(repo, ["one.py"])
        rendered = safe_commit_offer._render_report(
            _report(_group(paths=["one.py"], committed=True, sha=sha, push_state="ok")),
            str(repo),
        )

        assert scope.normalize_diagnostic_fired() is True
        assert rendered.splitlines()[0].startswith("DEGRADED INPUT")

    def test_banner_wording_claims_only_what_the_latch_implies(self, monkeypatch):
        """A set latch implies entries may have been DROPPED or MIS-NORMALIZED
        — i.e. the pathspec may be incomplete or name the wrong file. It does
        NOT imply anything was mis-committed, and it does not cover the
        out-of-repo case, which no longer arms the latch at all."""
        monkeypatch.setattr(scope, "_normalize_diag_fired", True)

        rendered = safe_commit_offer._render_report(_report(_group()))
        banner = rendered.splitlines()[0]

        assert "touch record" in banner and "may be" in banner
        assert "dropped or mis-normalized" in banner
        assert "Routine out-of-repo paths do not raise this." in banner
        assert "\n" not in safe_commit_offer._DEGRADED_SCOPE_NOTICE, (
            "the notice must stay ONE line — the outcome-last property assumes "
            "a bounded head, and a multi-line banner pushes the verdict down"
        )

    def test_accessor_does_not_reset_the_latch(self, monkeypatch):
        monkeypatch.setattr(scope, "_normalize_diag_fired", True)

        assert scope.normalize_diagnostic_fired() is True
        assert scope.normalize_diagnostic_fired() is True
        assert scope._normalize_diag_fired is True


# ---------------------------------------------------------------------------
# (i) memo.send declares its state/memo-outbox/ writes (2026-08-05
# engine-ops-declare-what-they-write plan, C1+C2) — proves the
# `_scope_touch_paths` self-report memo.send now sets actually reaches
# `compute_offer`'s `safe_paths`, driven through the REAL dispatch
# chokepoint (`ipc.dispatch_message`), not a synthetic test handler.
# Mirrors coordinator_core/tests/test_ipc_scope_touch_self_report.py's own
# "real handler, not synthetic" pattern (its tests 13/14), and reuses the
# receiver-repo/registry fixture shape from
# coordinator_core/ops/fleet/tests/test_memo_send.py (duplicated narrowly
# here rather than imported, to keep this test file's own fixtures
# self-contained).
# ---------------------------------------------------------------------------


def _make_memo_send_receiver_repo(tmp_path):
    root = tmp_path / "receiver-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True, **no_console_passthrough_kwargs())
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init receiver"], cwd=root, check=True, **no_console_passthrough_kwargs())
    return root


def _make_memo_send_claude_home(tmp_path, receiver_repo):
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    toml_val = str(receiver_repo).replace("\\", "\\\\").replace('"', '\\"')
    (machine_local / "registry.local.toml").write_text(
        f'"repos.example_retrieval_repo" = "{toml_val}"\n', encoding="utf-8"
    )
    return claude_home


class TestMemoSendDeclaresOutboxWrites:
    # The `designed_red` marker and its "blocked on the memo.send op
    # SUSPENSION" rationale are REMOVED (2026-08-27): memo.send is not on the
    # suspension roster -- `op_budget_suspension.py` does not name it -- so
    # that explanation was stale and the test was failing for an entirely
    # different reason it attributed to a mechanism that had moved on.
    #
    # The real cause: memo.send's wire contract narrowed to (dry_run, topic).
    # Every other field is read off the caller's already-staged
    # `state/memo-outbox/<topic>.md` draft, "never off the wire" (the op's own
    # refusal text). This test passed to/title/body/kind/summary as params and
    # was refused before it reached the behaviour it guards. Staging a real
    # draft is what the op actually asks for.
    def test_sent_ledger_write_lands_in_compute_offer_safe_paths(
        self, tmp_path, monkeypatch
    ):
        sender = tmp_path / "sender-repo"
        sender.mkdir()
        _make_repo(sender)
        receiver = _make_memo_send_receiver_repo(tmp_path)
        claude_home = _make_memo_send_claude_home(tmp_path, receiver)

        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "mine")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

        outbox = sender / "state" / "memo-outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "declare-touch-test.md").write_text(
            "---\n"
            "to: example-retrieval-repo-em\n"
            "from: claude-klabauter-em\n"
            "title: Test Memo\n"
            "kind: fyi\n"
            "summary: Test summary.\n"
            "---\n\n"
            "This is a test memo body.\n",
        )

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "memo.send",
            "params": {"dry_run": False, "topic": "declare-touch-test"},
            "_origin_worktree": str(sender),
        }
        d = asyncio.run(dispatch_message(msg))
        assert "error" not in d, d
        assert d["result"]["exit_code"] == 0

        ledger_rel = "state/memo-outbox/sent-ledger.jsonl"
        assert (sender / ledger_rel).is_file(), "sanity: the ledger must have actually been written"

        offer = safe_commit_offer.compute_offer("mine", cwd=str(sender))
        assert ledger_rel in offer["safe_paths"]


# ---------------------------------------------------------------------------
# (g) Mechanical-fallback commit framing. The `invoker` parameter that used to
# select between three framings was DELETED 2026-08-27 (no producer ever passed
# it; doe-claude-em confirmed neither surviving ceremony call site passes one or
# intends to). What these tests now lock is the ONE surviving shape and, more
# importantly, the claims it must never make.
#
# The originating incident still governs (example-cockpit-repo-em memo, 2026-08-17):
# a deliberate, curated ceremony commit landed in history confidently
# mislabelled an unattended stop-event rescue. Deleting the parameter does not
# reinstate that bug — it removes the vocabulary that made it expressible — so
# the negative assertions below outlive the parameter and are the real subject
# of this section.
# ---------------------------------------------------------------------------


class TestDefaultGroupsFraming:
    def test_body_asserts_how_never_why(self):
        groups = safe_commit_offer._default_groups(["a.py"], "sess123456")
        assert len(groups) == 1
        subject = groups[0]["message"]
        prose = groups[0]["prose"]

        # Short and bounded: count + subsystem key + short session id, never an
        # enumerated file list (unreadable in `git log --oneline`).
        assert subject == "auto-commit: 1 file(s) (session sess12, (repo root))"

        # It may say HOW the paths were bucketed...
        assert "asserts nothing about why it happened" in prose
        assert "subsystem-" in prose
        assert "  - a.py" in prose

    def test_no_stop_event_claim(self):
        """The retired `"unattended"` framing. No caller can substantiate it:
        the SessionEnd trigger is gone, and anything that can dial the op can
        reach this fallback. A subject claiming a rescue is a claim about WHY.
        """
        prose = safe_commit_offer._default_groups(["a.py"], "sess123456")[0]["prose"]
        subject = safe_commit_offer._default_groups(["a.py"], "sess123456")[0]["message"]
        assert "rescued at session stop" not in subject
        assert "Stop-event safety net" not in prose
        assert "ended without committing them itself" not in prose

    def test_no_deliberate_ceremony_claim(self):
        """The retired `"attended"` framing, and the half easier to lose: having
        removed the false "accident" story, do not default to the opposite false
        story. This function cannot know a ceremony ran.
        """
        prose = safe_commit_offer._default_groups(["a.py"], "sess123456")[0]["prose"]
        assert "ceremony" not in prose.lower()
        assert "deliberate" not in prose.lower()

    def test_takes_no_framing_argument(self):
        """The deletion itself. A third positional would silently be a framing
        selector again; this pins the signature so re-adding one is a test
        failure rather than a quiet regression.
        """
        import inspect

        params = list(inspect.signature(safe_commit_offer._default_groups).parameters)
        assert params == ["safe_paths", "session_id"]


class TestGroupsSuppliedPathKeepsAuthoredFraming:
    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). NOT the attribution kill -- that was
    # rebuilt and `_MECHANISM_DISABLED` is gone. This re-greens when the op is
    # proven under 2s and leaves the roster, and not before; nothing in this
    # module can lift it.
    @pytest.mark.designed_red
    def test_explicit_groups_reach_the_commit_verbatim(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer(
            "mine",
            str(repo),
            groups=[{"paths": ["a.py"], "message": "hand-authored subject"}],
        )
        assert len(report["groups"]) == 1
        assert report["groups"][0]["message"] == "hand-authored subject"
        # A caller that authored its own framing must not have the mechanical
        # fallback's wording grafted onto it — `_default_groups` was never called.
        assert "rescued at session stop" not in report["groups"][0]["message"]


class TestHandlerRejectsRetiredInvokerParam:
    def test_invoker_param_is_ignored_not_honoured(self):
        """A ceremony still passing the retired param must not silently get a
        framing back. The param is gone, so the call is refused for the reason
        it is actually deficient (identity), never quietly accepted with the
        old three-way behaviour resurrected.
        """
        out = safe_commit_offer._handler({"invoker": "unattended"})
        assert "caller identity could not be established" in out["error"]


class TestNothingToCommitDistinguishesSeenFromClean:
    """`state/bug-backlog/2026-08-20-safe-commit-offer-silently-drops-cli-wri-83abe919148c.yaml`.

    An empty `groups` had ONE rendering for two materially different states:
    a genuinely clean tree, and a tree whose every dirty path was seen and
    declined because nothing carried this session's claim. The second is the
    shape a CLI-written file always takes -- `hooks.track_touched_files`
    fires on Edit/Write/MultiEdit/NotebookEdit and nothing else, so a file a
    coordinator CLI wrote on the session's behalf reaches `compute_scope`
    only through the Step-2 mtime fallback, is routed to `mtime_only`, and
    is withheld by Step 4(c). Live instance: 1866 declined paths rendered
    alongside a bare "Nothing to commit for session <id>."
    """

    def test_declined_paths_are_named_as_seen_not_absent(self):
        report = _report(_group())
        report["groups"] = []
        report["excluded"] = [
            {"path": "state/sizings/x.yaml", "reason": "untouched by this session"}
        ]

        rendered = safe_commit_offer._render_report(report)

        assert "Nothing to commit for session mine" in rendered
        assert "seen and declined" in rendered
        assert "working tree clean" not in rendered
        assert "coordinator-safe-commit" in rendered, (
            "an operator told nothing was committed must be given the route "
            "that does commit it by name"
        )

    def test_clean_tree_says_clean(self):
        report = _report(_group())
        report["groups"] = []
        report["excluded"] = []

        rendered = safe_commit_offer._render_report(report)

        assert "working tree clean" in rendered
        assert "seen and declined" not in rendered


class TestFullOwnershipMap:
    """The in-process map a dirty-tree sweep classifies against -- deliberately
    NOT the same answer as `compute_offer`'s ownership readout."""

    def test_names_a_peers_path_this_session_never_touched(self, tmp_path):
        # The regression that made this exist: read off `compute_offer`, a
        # peer's in-flight file the closing session never touched is in no
        # bucket at all, so a sweep classifies it AMBIGUOUS and an operator is
        # nudged toward committing it.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "theirs.py").write_text("t")
        scope.touch("peer", "theirs.py", cwd=str(repo))

        mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))

        assert "theirs.py" not in mine
        assert peer_map["theirs.py"]["owner"] == "peer"
        assert peer_map["theirs.py"]["liveness"] == "live"

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "theirs.py" not in {e["path"] for e in offer["ownership"]["peer"]}

    def test_mine_is_the_sole_claims_and_contested_is_a_peer_entry(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "solo.py").write_text("s")
        (repo / "shared.py").write_text("h")
        scope.touch("mine", "solo.py", cwd=str(repo))
        scope.touch("mine", "shared.py", cwd=str(repo))
        scope.touch("peer", "shared.py", cwd=str(repo))

        mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))

        assert mine == frozenset({"solo.py"})
        assert peer_map["shared.py"]["owner"] == "peer"

    def test_an_unclaimed_path_is_in_neither(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "nobodys.py").write_text("n")

        mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))

        assert "nobodys.py" not in mine
        assert "nobodys.py" not in peer_map

    def test_a_degraded_walk_empties_the_peer_map(self, tmp_path, monkeypatch):
        # AC7 again, at this surface: a call that could not finish its walk
        # must not print an owner. An empty map degrades the caller to "no
        # claim awareness", which is its own fail-closed arm.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "theirs.py").write_text("t")
        scope.touch("peer", "theirs.py", cwd=str(repo))

        # C7b: retargeted at `_read_stream_claims` -- see the sibling
        # blinding test above for why.
        blinded = os.path.join(
            core.session_dir("peer", cwd=str(repo)), scope._TOUCH_RECORD_FILENAME
        )
        real_reader = claim_index._read_stream_claims

        def _unreadable(path):
            if os.path.normcase(str(path)) == os.path.normcase(blinded):
                return {}, False
            return real_reader(path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)

        mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))

        assert peer_map == {}

    def test_spawns_no_subprocess(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "theirs.py").write_text("t")
        scope.touch("peer", "theirs.py", cwd=str(repo))
        safe_commit_offer.full_ownership_map("mine", cwd=str(repo))  # warm caches

        def _explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("full_ownership_map spawned a subprocess")

        monkeypatch.setattr(subprocess, "run", _explode)
        monkeypatch.setattr(subprocess, "Popen", _explode)
        monkeypatch.setattr(subprocess, "check_output", _explode)

        _mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))
        assert "theirs.py" in peer_map


    def test_liveness_is_resolved_once_not_once_per_peer(self, tmp_path, monkeypatch):
        """The amplification pin. `live_session_ids` is deliberately NOT
        memoised (a cached live-set reopens the wrong-attribution race, per
        its own negative spec), so calling it per entry re-walks every session
        dir each time. Measured on the real ledger before the hoist, job
        object, k=20: 23,910ms of process time across ~405 peer claims, in
        ZERO subprocesses -- 48x the brightline, and per-item amplification of
        exactly the shape this module was rebuilt to remove.

        Counted, not timed: a wall-clock assertion here would measure peer load
        on a box carrying ~50 sessions. The call COUNT is the invariant.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        for i in range(12):
            core.init("peer-%02d" % i, cwd=str(repo))
            rel = "theirs_%02d.py" % i
            (repo / rel).write_text(str(i))
            scope.touch("peer-%02d" % i, rel, cwd=str(repo))

        calls = []
        real = safe_commit_offer.live_session_ids

        def _counted(cwd=None):
            calls.append(cwd)
            return real(cwd)

        monkeypatch.setattr(safe_commit_offer, "live_session_ids", _counted)

        _mine, peer_map = safe_commit_offer.full_ownership_map("mine", cwd=str(repo))

        assert len(peer_map) == 12
        assert len(calls) == 1, (
            "liveness resolved %d times for %d peer paths -- it must be "
            "resolved once and answered from the set" % (len(calls), len(peer_map))
        )


class TestReconciliation:
    """C1/C2/C3 (docs/plans/2026-08-29-the-ledger-stops-asserting-what-it-did-
    not-check.md) — the write-only ledger stops asserting what it did not
    check.

    The three cases are the two drift directions plus the honesty flag, and
    they are separate tests deliberately: the drifts have different causes
    (an unrecorded write vs an unobserved deletion) and a single fixture
    exercising both would let one regress while the other kept the test
    green.

    Fixture shape is the one the 2026-08-29 spike used live
    (docs/research/spike-verdicts/2026-08-29-bash-writes-reach-the-touch-
    ledger.md): a claimed path, a claimed-then-deleted path, and a dirty path
    nothing ever claimed. The spike's own probe scripts were throwaway and
    were deleted — this class is the durable guard, which is the plan's job
    and never the spike's.
    """

    def test_a_path_nothing_claims_is_enumerated_not_merely_counted(self, tmp_path):
        """The load-bearing property is ENUMERATION, not rendering.

        doe-claude-em's SC-DR-022 half 1 permits an operator to adopt an
        unclaimed path with `--include-orphans`, and the property that makes
        that remedy safe is that the candidate paths are named BY THE ENGINE,
        never assembled by the adopter. An aggregate count supplies no
        candidate list, so the remedy would have no input. This is the
        regression guard for that: the path must be a member, not a number.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "claimed.py", cwd=str(repo))
        (repo / "claimed.py").write_text("mine")
        # Written the way a shell heredoc writes: no claim is ever recorded.
        (repo / "shell_written.py").write_text("nobody claimed me")

        _residue, rec = safe_commit_offer._compute_residue("mine", [], str(repo))

        assert rec["reconciled"] is True
        assert "shell_written.py" in rec["unclaimed"]
        # This session's OWN claimed-and-uncommitted work is not an adoption
        # candidate — folding the two together is what would make the
        # enumeration unsafe to hand to `--include-orphans`.
        assert "claimed.py" not in rec["unclaimed"]

    def test_a_claimed_path_deleted_from_disk_is_named_not_silently_counted(
        self, tmp_path
    ):
        """The over-record direction, and the shape that produced the P1 row.

        Observed live 2026-08-26: `ownership.mine: 1, degraded: false` for a
        file that did not exist — a subagent wrote it (ledger recorded the
        claim), then a later step consumed and deleted it (the ledger, being
        write-only, never saw that). Reproduced here without the subagent.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "transient.py").write_text("here for now")
        scope.touch("mine", "transient.py", cwd=str(repo))
        (repo / "transient.py").unlink()

        _residue, rec = safe_commit_offer._compute_residue("mine", [], str(repo))

        assert "transient.py" in rec["claimed_absent"]

    def test_reconciled_is_a_did_the_check_run_flag_never_a_health_flag(
        self, tmp_path
    ):
        """`reconciled` must stay orthogonal to whether anything was found.

        A repo that closes queue entries by `git mv` leaves an unclaimed
        dirty deletion at the source forever (doe-claude-em, 2026-08-29,
        citing their SC-DR-021 d1), so a `reconciled` that meant "ledger and
        tree agree" would read red in steady state — and a signal that is
        always red is a signal nobody reads. It means only that the check
        ran.

        The negative half matters as much: with no worktree root there is
        nothing to check against, and the answer must say UNCHECKED rather
        than returning empty buckets that read as "nothing found".
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "transient.py").write_text("x")
        scope.touch("mine", "transient.py", cwd=str(repo))
        (repo / "transient.py").unlink()

        _residue, found = safe_commit_offer._compute_residue("mine", [], str(repo))
        assert found["reconciled"] is True
        assert found["claimed_absent"]  # a finding does NOT flip the flag

        _residue, unchecked = safe_commit_offer._compute_residue("mine", [], None)
        assert unchecked["reconciled"] is False
        assert unchecked["claimed_absent"] == []
        assert unchecked["unclaimed"] == []

    def test_degraded_and_reconciled_are_independent_signals(self, tmp_path):
        """Guards the Anti-scope rule that these must not be fused.

        `degraded` is `not CommitSet.complete` — a statement about the claim
        ledger's own readability. It carries a second, load-bearing
        consequence (when set, `ownership.peer` is emptied and every non-mine
        path folds into `unattributed`, AC7), which is exactly why
        reconciliation must not be folded into it: a stale-but-readable
        ledger would then silently trigger that fold on a healthy walk.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "transient.py").write_text("x")
        scope.touch("mine", "transient.py", cwd=str(repo))
        (repo / "transient.py").unlink()

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        _residue, rec = safe_commit_offer._compute_residue("mine", [], str(repo))

        # A complete walk over a ledger that disagrees with the tree.
        assert offer["ownership"]["degraded"] is False
        assert rec["reconciled"] is True
        assert rec["claimed_absent"] == ["transient.py"]


class TestSpawnBudget:
    """The module docstring's § BUDGET, pinned as a test rather than prose.

    A budget nothing checks is a number that drifts. This is the DR-344
    surface for this op: `session.safe_commit_offer` sits on the commit hot
    path every close ceremony crosses, so a spawn that starts scaling with
    the path or group count is per-item amplification reappearing in the
    exact module the 2026-08-21 rebuild removed it from.

    Counts `subprocess.Popen` rather than process time: process time on a
    box carrying ~50 concurrent sessions is a floor by construction
    (`op_budget_suspension.py`'s rule for git-spawning ops), so it cannot be
    asserted on; the spawn COUNT is deterministic and is the axis that
    actually governs the cost.
    """

    @staticmethod
    def _commit_with_tally(repo, sid, n_files, n_groups, monkeypatch):
        paths = []
        for i in range(n_files):
            rel = "f%02d.txt" % i
            (repo / rel).write_text("body %d" % i)
            scope.touch(sid, rel, cwd=str(repo))
            paths.append(rel)

        tally = []
        real_popen = subprocess.Popen

        class CountingPopen(real_popen):
            def __init__(self, args, *a, **kw):
                tally.append(args if isinstance(args, str) else list(args))
                super().__init__(args, *a, **kw)

        monkeypatch.setattr(subprocess, "Popen", CountingPopen)
        per = max(1, len(paths) // n_groups)
        groups = [
            {"paths": paths[i:i + per], "message": "budget group %d" % (i // per)}
            for i in range(0, len(paths), per)
        ]
        report = safe_commit_offer.commit_session_offer(sid, str(repo), groups)
        return report, tally

    @pytest.mark.parametrize("n_files,n_groups", [(4, 4), (12, 3), (40, 8)])
    def test_one_git_spawn_regardless_of_paths_or_groups(
        self, tmp_path, monkeypatch, n_files, n_groups
    ):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report, tally = self._commit_with_tally(
            repo, "mine", n_files, n_groups, monkeypatch
        )

        assert report["outcome"]["status"] == "committed"
        assert len(report["outcome"]["committed_paths"]) == n_files
        assert report["failed_groups"] == []
        assert len(tally) == 1, "spawn budget is ONE; got %r" % (tally,)

    def test_the_one_spawn_is_the_residue_read(self, tmp_path, monkeypatch):
        """Names WHICH spawn the budget is spent on.

        A count alone would stay green if `_current_dirty_paths`'s read were
        swapped for some other single git call — and the residue read is the
        one this module can justify (its `residue`/`reconciliation` products
        are read back by `quick_wrap_assemble`), so the identity is the
        property worth pinning, not just the arithmetic.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        _report, tally = self._commit_with_tally(repo, "mine", 3, 1, monkeypatch)

        assert len(tally) == 1
        argv = tally[0]
        assert argv[0] == "git"
        assert "status" in argv
        assert "--porcelain" in argv
        assert "--untracked-files=all" in argv
