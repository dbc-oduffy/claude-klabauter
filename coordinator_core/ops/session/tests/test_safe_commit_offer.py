"""
coordinator_core.ops.session.tests.test_safe_commit_offer

Tests for coordinator_core.ops.session.safe_commit_offer — the unattended
stop-event auto-commit+push mechanism (compute_offer, path normalization,
auto_commit_session, and the CLI main()).

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
  (e) auto_commit_session — NO confirmation step (direct call lands a real
      commit); default mechanical grouping; explicit `--message` single
      group; explicit `groups` never widens past computed safe_paths (a
      peer-owned path smuggled into a caller-supplied group is dropped, not
      committed); empty safe_paths is a valid no-op ("nothing to commit").
  (f) CLI — --dry-run (no commit), --message, --groups-json, mutually
      exclusive flags, unresolvable-session exit 1, usage exit 2.

Class B: substrate is untracked .git/coordinator-sessions/ — commit
assertions here ARE made (auto_commit_session is the one mutating surface
this module owns), scoped to the test's own tmp_path fixture repo, never
the real corpus.

Spec backlink: coordinator_core/ops/session/safe_commit_offer.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import dispatch_message
from coordinator_core.session import core, scope
from coordinator_core.ops.session import safe_commit_offer

# Real git spawn is load-bearing: compute_offer's dirty-set math and
# `_dirty_files_under` read actual `git status`/diff output, and
# auto_commit_session lands real commits under test — no mock stands in for
# git's own path-classification and index state here.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    # Review: staff-eng F12 — check=True on every fixture-setup git call
    # (mirrors test_scope.py's _make_repo): a silent fixture-setup failure
    # must not masquerade as a passing test.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


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

    def test_unclaimed_dirty_since_start_is_declined_not_adopted(self, tmp_path):
        """Prior name/assertion (`..._is_safe_not_excluded`) asserted the
        OLD mtime-fallback disposition: an uncontested dirty file with no
        touched.txt record anywhere silently joined `safe_paths` merely by
        having a recent mtime. Inverted per DR-246 (2026-07-31) /
        docs/plans/2026-07-31-unclaimed-dirty-file-adoption.md: `compute_scope`
        Step 4 now routes a candidate that entered ONLY via the Step-2 mtime
        scan to `orphans`, so it is withheld from `safe_paths` and reported
        as an excluded orphan instead."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "orphan.py").write_text("o")
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert offer["safe_paths"] == []
        assert {"path": "orphan.py", "reason": "untouched by this session"} in offer["excluded"]

    def test_orphan_before_session_start_excluded_untouched(self, tmp_path):
        import os
        import time

        repo = _make_repo(tmp_path)
        (repo / "old.py").write_text("z")
        old_mtime = time.time() - 10_000
        os.utime(repo / "old.py", (old_mtime, old_mtime))
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text(core.now_iso())
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "old.py" not in offer["safe_paths"]
        assert {"path": "old.py", "reason": "untouched by this session"} in offer["excluded"]

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

    def test_unreadable_peer_touched_dedupes_to_one_excluded_entry(
        self, tmp_path, monkeypatch
    ):
        # Review: code-reviewer (Finding 2) — a candidate withheld via
        # `unreadable_other_sessions` (compute_scope Step 4) lands in BOTH
        # `skipped` (owner "unknown (claims unreadable: ...)") and `orphans`
        # (absent from my_scope, and not actually claimed) -- the dedupe in
        # `compute_offer` must collapse this to exactly ONE `excluded` entry,
        # carrying the more specific unreadable-owner reason.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")

        other_touched = Path(core.session_dir("other", cwd=str(repo))) / "touched.txt"
        other_touched.write_text("shared.py\n")  # "other" actually owns it
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == other_touched:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        matches = [e for e in offer["excluded"] if e["path"] == "shared.py"]
        assert len(matches) == 1
        assert matches[0]["reason"] == "owned by session unknown (claims unreadable: other)"

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

    def test_genuine_uncontested_orphan_still_appears_filter_not_overbroad(
        self, tmp_path
    ):
        """Companion to the test above: pins that the F1 subtraction is
        NOT over-broad — a genuine orphan (dirty, claimed by nobody,
        withheld for no OTHER reason) must still surface in
        ``offer["orphans"]``. Without this, a regression that simply made
        ``compute_offer`` always return ``orphans: []`` would pass the
        "does not resurface" test above vacuously.

        Same fixture shape as
        ``test_mtime_fallback_orphan_adoption_is_now_declined_by_compute_scope``
        (a dirty file with no ``touched.txt`` record anywhere, adopted only
        via the Step-2 mtime scan) — single session, no peer, so
        ``unreadable_other_sessions``/``agent_race_paths`` stay empty and
        ``indeterminate`` is genuinely False, isolating the F1 subtraction's
        behavior from the R1 whole-call gate.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        (repo / "unclaimed_by_anyone.py").write_text("x")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["indeterminate"] is False
        assert offer["orphans"] == ["unclaimed_by_anyone.py"]

    def test_indeterminate_call_withholds_orphans_outright(self, tmp_path, monkeypatch):
        """Staff-eng R1 (2026-08-03, re-review pass 2) — the related
        whole-call withhold: on an indeterminate call (here, an unreadable
        peer claim set), ``orphans`` comes back EMPTY OUTRIGHT, not just
        minus the specific withheld candidate. Checked before writing:
        ``indeterminate`` itself is asserted at the ``compute_scope`` layer
        (`coordinator_core/session/tests/test_scope.py::
        test_indeterminate_...`) and at the ``compute_offer`` layer only for
        the trivially-empty-everything case
        (`test_empty_scope_is_a_valid_result`) — this module has no test
        pinning the wider blast radius on a call that ALSO has a genuine,
        unrelated orphan candidate, so this is new coverage, not a
        duplicate.

        ``unrelated_orphan.py`` is never a candidate touched by anyone and
        is not the path that triggered the unreadable claim (``shared.py``)
        — it would appear in ``orphans`` on its own (per the test above) if
        this call were NOT indeterminate. Asserting it is ALSO withheld here
        proves the gate is call-wide, not just a per-path subtraction.
        """
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "shared.py").write_text("s")
        (repo / "unrelated_orphan.py").write_text("u")

        other_touched = Path(core.session_dir("other", cwd=str(repo))) / "touched.txt"
        other_touched.write_text("shared.py\n")
        orig_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == other_touched:
                raise OSError("simulated read failure")
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))

        assert offer["indeterminate"] is True
        assert offer["orphans"] == []

    def test_read_only_no_git_or_touched_mutation(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))
        touched_file = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        before = touched_file.read_text()
        status_before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert touched_file.read_text() == before
        status_after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
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
        (agent / "touched.txt").write_text("coordinator/agent_file.py\n")
        agent_touched_file = agent / "touched.txt"
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
        (my_agent / "touched.txt").write_text("coordinator/mine_file.py\n")

        sibling_agent = _agent_dir(repo, "aid-sibling")
        (sibling_agent / "em-session-id.txt").write_text("sibling-em")
        (sibling_agent / "touched.txt").write_text("coordinator/sibling_file.py\n")

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
        (my_agent / "touched.txt").write_text("coordinator/mine_file.py\n")

        sibling_agent = _agent_dir(repo, "aid-sibling")
        (sibling_agent / "em-session-id.txt").write_text("sibling-em")
        (sibling_agent / "touched.txt").write_text("coordinator/sibling_file.py\n")

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
        (agent / "touched.txt").write_text(
            "T 2026-08-03T23:18:56.014950Z coordinator/kept.py\n"
            "T 2026-08-03T23:18:57.000000Z coordinator/released.py\n"
            "R 2026-08-03T23:20:00.000000Z coordinator/released.py\n"
        )

        candidates = safe_commit_offer._resolve_agent_touched_candidates(
            "mine", cwd=str(repo)
        )
        assert candidates == ["coordinator/kept.py"]
        assert not any(c.startswith("T ") or c.startswith("R ") for c in candidates)

    def test_mtime_fallback_orphan_adoption_is_now_declined_by_compute_scope(self, tmp_path):
        """Formerly `..._orphan_adoption_is_compute_scope_not_this_module`,
        which documented (did NOT "fix" -- PM-accepted collateral at the
        time) a hazard: a dirty file with NO touched.txt record ANYWHERE
        (crashed peer, pruned/rotated record, a session that never ran the
        touch hook) was indistinguishable, to `compute_scope`'s own
        pre-existing Step-2 mtime fallback, from "a file I touched but the
        hook missed recording" -- if its mtime was >= my own started_at, it
        silently joined MY safe_paths.

        SUPERSEDED (DR-246, 2026-07-31 --
        docs/plans/2026-07-31-unclaimed-dirty-file-adoption.md): C1a/C8
        narrowed `compute_scope` Step 4 so a candidate that entered the
        candidate set ONLY via the Step-2 mtime scan is routed to
        `orphans`, not `my_scope` -- an unowned, mtime-only dirty file is
        DECLINED adoption and reported as excluded ("untouched by this
        session") rather than silently committed under a stranger's
        session. Still orthogonal to the broadened/exact choice on
        `my_agent_touched` (which only governs sub-agent fan-out, never
        mtime-fallback candidates). Kept (inverted, not deleted) as the
        paper trail that this was a known, deliberately-revisited hazard."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        sdir = Path(core.session_dir("mine", cwd=str(repo)))
        (sdir / "started_at").write_text("2000-01-01T00:00:00Z")
        # A dirty file with NO touched.txt record anywhere -- simulating a
        # peer that crashed/rotated before recording it.
        (repo / "unclaimed_by_anyone.py").write_text("x")

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "unclaimed_by_anyone.py" not in offer["safe_paths"]  # declined, by design
        assert {
            "path": "unclaimed_by_anyone.py",
            "reason": "untouched by this session",
        } in offer["excluded"]


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
# (e) auto_commit_session — the mutating half, no confirmation
# ---------------------------------------------------------------------------


class TestAutoCommitSession:
    def test_no_confirmation_step_lands_a_real_commit(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is True
        assert report["groups"][0]["sha"]
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "a.py" not in log  # commit landed, message is the mechanical subject
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert status == ""  # nothing left dirty

    def test_commit_group_calls_the_sync_handler_without_awaiting_it(self, tmp_path):
        """Regression guard, 2026-08-07: `_commit_group` awaited the dict
        `ceremony.scoped_git_commit`'s handler returns, so EVERY invocation
        of `safe-commit-offer` died with `TypeError: object dict can't be
        used in 'await' expression` (cross-repo/inbox/2026-08-07-project-
        rag-em-safe-commit-offer-await-dict-typeerror.md). That op's handler
        is a plain sync `def` deliberately (3241c7c95) -- pinned here, since
        the call site now depends on it: if the op is ever made `async`
        again, this fails loudly rather than the composer failing silently
        at every session close."""
        import inspect

        from coordinator_core.ipc import get_op_handler

        handler = get_op_handler("ceremony.scoped_git_commit")
        assert handler is not None
        assert not inspect.iscoroutinefunction(handler)

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

    def test_default_grouping_subject_stays_bounded_body_carries_the_list(self, tmp_path):
        """Regression guard for the enumerated-filenames-in-subject shape:
        the subject must stay short/bounded regardless of file count, and
        the full path list + safety-net framing must land in the commit
        BODY, not the subject. Passes ``invoker="unattended"`` explicitly —
        the real SessionEnd-hook shape this test exercises; see
        TestDefaultGroupsInvokerFraming for the attended/undeclared
        counterparts, which do not carry safety-net framing."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        paths = []
        for i in range(12):
            p = "sub/file_%02d.py" % i
            (repo / "sub").mkdir(exist_ok=True)
            (repo / p).write_text(str(i))
            scope.touch("mine", p, cwd=str(repo))
            paths.append(p)

        report = safe_commit_offer.auto_commit_session(
            "mine", cwd=str(repo), invoker="unattended"
        )

        assert len(report["groups"]) == 1
        subject = report["groups"][0]["message"]
        assert len(subject) < 100
        assert "file_00.py" not in subject  # no enumerated filenames in the subject

        full_message = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True
        ).stdout
        for p in paths:
            assert p in full_message  # every rescued path IS in the body
        assert "safety net" in full_message.lower()

    def test_nothing_to_commit_is_a_valid_noop(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))
        assert report["groups"] == []
        assert report["excluded"] == []

    def test_explicit_groups_never_widen_past_safe_paths(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "a.py").write_text("a")
        (repo / "peer.py").write_text("p")
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["a.py", "peer.py"], "message": "smuggled group"}],
        )

        assert len(report["groups"]) == 1
        assert report["groups"][0]["paths"] == ["a.py"]  # peer.py silently dropped
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "peer.py" in status  # still dirty, never committed

    def test_group_with_only_excluded_paths_is_dropped_entirely(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session(
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

        report = safe_commit_offer.auto_commit_session(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["peer.py"], "message": "all-excluded group"}],
        )
        assert report["dropped_groups"] == [
            {"message": "all-excluded group", "named": 1, "matched": 0}
        ]

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

        report = safe_commit_offer.auto_commit_session(
            "mine",
            cwd=str(repo),
            groups=[{"paths": ["a.py", "peer.py"], "message": "partial group"}],
        )
        assert report["dropped_groups"] == [
            {"message": "partial group", "named": 2, "matched": 1}
        ]
        assert report["groups"][0]["paths"] == ["a.py"]

    def test_default_groups_never_populate_dropped_groups(self, tmp_path):
        # `groups=None` (the unattended-trigger fallback) is computed FROM
        # `safe_paths` itself, so it can never lose a path to the filter --
        # `dropped_groups` must stay empty for that path.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))
        assert report["dropped_groups"] == []

    def test_message_flag_produces_one_group(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        (repo / "b.py").write_text("b")
        scope.touch("mine", "a.py", cwd=str(repo))
        scope.touch("mine", "b.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session(
            "mine", cwd=str(repo), groups=[{"paths": ["a.py", "b.py"], "message": "one subject"}]
        )
        assert len(report["groups"]) == 1
        assert report["groups"][0]["message"] == "one subject"
        assert set(report["groups"][0]["paths"]) == {"a.py", "b.py"}

    def test_explicit_group_prose_reaches_the_commit_body(self, tmp_path):
        # Review: code-reviewer (Finding 4) — the explicit-`groups` branch of
        # auto_commit_session_async previously rebuilt each group without
        # carrying `g.get("prose")` through, so caller-supplied body text was
        # silently dropped and only the mechanical `_default_groups` fallback
        # ever produced a non-empty commit body.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        safe_commit_offer.auto_commit_session(
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
            ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "curated body text with real judgment behind it" in full_message

    def test_nothing_to_commit_noop_has_no_failed_groups(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))
        assert report["failed_groups"] == []

    def test_commit_failure_surfaces_via_failed_groups_not_swallowed(self, tmp_path, monkeypatch):
        """2026-07-31 fix: `auto_commit_session_async` previously swallowed a
        genuine `commit_failed` group -- `_commit_group` only ever read
        `result.get("error")`, which is None on a gate/commit failure (that
        shape carries `commit_failed`/`diagnostics` instead). A real failure
        must be surfaced through `AutoCommitReport.failed_groups`, distinct
        from the benign already-committed no-op shape (next test)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def fake_handler(params):
            return {
                "committed": False,
                "sha": None,
                "pushed": False,
                "commit_failed": True,
                "diagnostics": ["dirty-tree gate: unattributable paths: peer.py"],
            }

        def fake_get_op_handler(name):
            assert name == "ceremony.scoped_git_commit"
            return fake_handler

        monkeypatch.setattr("coordinator_core.ipc.get_op_handler", fake_get_op_handler)

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is False
        assert report["groups"][0]["commit_failed"] is True
        assert report["groups"][0]["error"]
        assert report["failed_groups"] == [report["groups"][0]]

    def test_benign_already_committed_noop_group_does_not_cry_wolf(self, tmp_path, monkeypatch):
        """The ordinary already-committed no-op (`commit_failed: False`,
        `reason: "empty-commit-set"`) must stay quiet -- it must NOT appear
        in `failed_groups`, and `error` must stay `None` (same wolf-crying
        constraint `TestRefusalReporting` pins for the CLI-rendering layer,
        one layer up)."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def fake_handler(params):
            return {
                "committed": False,
                "sha": None,
                "pushed": None,
                "commit_failed": False,
                "diagnostics": [],
                "reason": "empty-commit-set",
            }

        monkeypatch.setattr(
            "coordinator_core.ipc.get_op_handler", lambda name: fake_handler
        )

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["committed"] is False
        assert report["groups"][0]["commit_failed"] is False
        assert report["groups"][0]["error"] is None
        assert report["failed_groups"] == []

    def test_unregistered_handler_is_a_genuine_failure_not_a_quiet_noop(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        monkeypatch.setattr("coordinator_core.ipc.get_op_handler", lambda name: None)

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["commit_failed"] is True
        assert report["failed_groups"] == [report["groups"][0]]

    def test_handler_level_validation_error_is_a_genuine_failure(self, tmp_path, monkeypatch):
        # Review: code-reviewer (Finding 2) — the `if not committed and error
        # and not commit_failed` defensive branch in `_commit_group` had no
        # test exercising a handler-level validation error (`error` set
        # directly, no `commit_failed`/`diagnostics` keys at all).
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def fake_handler(params):
            return {"committed": False, "error": "some validation error"}

        monkeypatch.setattr(
            "coordinator_core.ipc.get_op_handler", lambda name: fake_handler
        )

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["commit_failed"] is True
        assert report["groups"][0]["error"] == "some validation error"
        assert report["failed_groups"] == [report["groups"][0]]

    def test_reason_threaded_through_group_result(self, tmp_path, monkeypatch):
        # Review: code-reviewer (Finding 3) — `reason` (e.g.
        # "empty-commit-set") is computed by the op but was dropped before
        # reaching `GroupResult`.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def fake_handler(params):
            return {
                "committed": False,
                "commit_failed": False,
                "reason": "empty-commit-set",
            }

        monkeypatch.setattr(
            "coordinator_core.ipc.get_op_handler", lambda name: fake_handler
        )

        report = safe_commit_offer.auto_commit_session("mine", cwd=str(repo))

        assert report["groups"][0]["reason"] == "empty-commit-set"
        rendered = safe_commit_offer._render_report(report)
        assert "empty-commit-set" in rendered


# ---------------------------------------------------------------------------
# (f) CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_dry_run_computes_without_committing(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        exit_code = safe_commit_offer.main(
            ["--session", "mine", "--root", str(repo), "--dry-run", "--json"]
        )
        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["safe_paths"] == ["a.py"]
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "a.py" in status  # NOT committed

    def test_message_flag_via_cli_commits(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        exit_code = safe_commit_offer.main(
            ["--session", "mine", "--root", str(repo), "--message", "hand-authored subject"]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "hand-authored subject" in out
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert status == ""

    def test_groups_json_flag(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        groups_file = tmp_path / "groups.json"
        groups_file.write_text(json.dumps([{"paths": ["a.py"], "message": "from json"}]))

        exit_code = safe_commit_offer.main(
            ["--session", "mine", "--root", str(repo), "--groups-json", str(groups_file)]
        )
        assert exit_code == 0
        assert "from json" in capsys.readouterr().out

    def test_message_and_groups_json_mutually_exclusive(self, tmp_path):
        exit_code = safe_commit_offer.main(
            ["--message", "x", "--groups-json", "y.json"]
        )
        assert exit_code == 2

    def test_unresolvable_session_exits_1(self, tmp_path, capsys, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        exit_code = safe_commit_offer.main(["--root", str(repo)])
        assert exit_code == 1
        assert "could not resolve" in capsys.readouterr().err

    def test_all_benign_noop_run_exits_0_not_4(self, tmp_path, capsys):
        # Regression guard: the ordinary already-committed no-op must NEVER
        # be conflated with a genuine `commit_failed` group -- `git commit`
        # itself exits 1 on an empty commit set, so a version that cried
        # wolf here would fire on every ordinary session end.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        exit_code = safe_commit_offer.main(["--session", "mine", "--root", str(repo)])
        assert exit_code == 0

    def test_genuine_commit_failure_exits_4_and_logs_diagnostic(
        self, tmp_path, capsys, monkeypatch
    ):
        # Review: code-reviewer (Finding 1) — `failed_groups` was computed
        # and tested but never surfaced anywhere a real caller (the
        # SessionEnd hook, which only inspects the subprocess exit code)
        # reads. Exit code 4 plus an in-process diagnostic write closes that
        # gap without requiring a change on the hook's side.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        def fake_handler(params):
            return {
                "committed": False,
                "commit_failed": True,
                "diagnostics": ["dirty-tree gate: unattributable paths: peer.py"],
            }

        monkeypatch.setattr(
            "coordinator_core.ipc.get_op_handler", lambda name: fake_handler
        )

        exit_code = safe_commit_offer.main(["--session", "mine", "--root", str(repo)])
        assert exit_code == 4

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

    def test_usage_error_exits_2(self, tmp_path, capsys):
        exit_code = safe_commit_offer.main(["--bogus-flag"])
        assert exit_code == 2

    def test_declined_adoption_logs_diagnostic_and_stays_exit_0(self, tmp_path, capsys):
        # Review: code-reviewer (Finding 1) — `_log_excluded_diagnostic` is
        # the headline AC6 deliverable and had zero test coverage. A dirty
        # unclaimed file must be logged AND must leave the exit code at 0 —
        # a declined adoption is the correct outcome (DR-227), never a
        # failure.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "orphan.py").write_text("o")

        exit_code = safe_commit_offer.main(["--session", "mine", "--root", str(repo)])
        assert exit_code == 0

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        assert log_file.is_file()
        contents = log_file.read_text()
        assert "declined adoption" in contents
        assert "orphan.py" in contents
        assert "untouched by this session" in contents

    def test_declined_adoption_preview_bound_enforced_on_written_log(self, tmp_path, capsys):
        # Review: code-reviewer (Finding 1) — proves the preview bound is
        # enforced on what is WRITTEN to the log, not merely on a rendered
        # string: only `_EXCLUDED_LOG_PREVIEW_COUNT` path lines appear, and
        # the remainder is named via an "and N more" tail.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        total = safe_commit_offer._EXCLUDED_LOG_PREVIEW_COUNT + 3
        paths = []
        for i in range(total):
            p = "orphan_%02d.py" % i
            (repo / p).write_text(str(i))
            paths.append(p)

        exit_code = safe_commit_offer.main(["--session", "mine", "--root", str(repo)])
        assert exit_code == 0

        from coordinator_core.lifecycle import git_common_dir

        log_file = (
            git_common_dir(Path(repo))
            / "coordinator-sessions"
            / "logs"
            / "sessionend-auto-commit-diagnostics.log"
        )
        contents = log_file.read_text()
        assert "... and 3 more" in contents
        present = [p for p in paths if p in contents]
        assert len(present) == safe_commit_offer._EXCLUDED_LOG_PREVIEW_COUNT

    def test_all_dropped_group_renders_named_matched_and_logs_and_stays_exit_0(
        self, tmp_path, capsys
    ):
        # Handoff item 1 (2026-08-03, touched-path-bookkeeping) -- an
        # all-dropped caller-supplied group was previously silent: absent
        # from `groups`, `failed_groups`, AND `excluded` (which is
        # `compute_offer`-derived, not group-derived) all at once. This is
        # the regression guard for the fix: the stdout render, the
        # diagnostics-log sink, and the exit code all reflect it, and it is
        # never reported as a commit failure.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "peer.py").write_text("p")
        scope.touch("other", "peer.py", cwd=str(repo))

        groups_path = tmp_path / "groups.json"
        groups_path.write_text(
            json.dumps([{"paths": ["peer.py"], "message": "all-excluded group"}])
        )

        exit_code = safe_commit_offer.main(
            [
                "--session",
                "mine",
                "--root",
                str(repo),
                "--groups-json",
                str(groups_path),
            ]
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "all-excluded group — named 1 paths, 0 matched" in out
        assert "NOT committed" not in out

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

    def test_dropped_groups_render_and_log_are_bounded(self, tmp_path, capsys):
        # Bounded-output guard: many dropped groups must still render (and
        # log) a bounded number of lines plus an "and N more group(s)" tail
        # -- never one line per group unbounded, the same shape the 1938-
        # entry `excluded` incident this module already retired once for a
        # different field.
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))

        total = safe_commit_offer._DROPPED_GROUPS_PREVIEW_COUNT + 3
        groups = []
        for i in range(total):
            p = "peer_%02d.py" % i
            (repo / p).write_text(str(i))
            scope.touch("other", p, cwd=str(repo))
            groups.append({"paths": [p], "message": "dropped group %02d" % i})

        groups_path = tmp_path / "groups.json"
        groups_path.write_text(json.dumps(groups))

        exit_code = safe_commit_offer.main(
            [
                "--session",
                "mine",
                "--root",
                str(repo),
                "--groups-json",
                str(groups_path),
            ]
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "... and 3 more group(s)" in out
        shown = [g for g in groups if g["message"] in out]
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
        logged = [g for g in groups if g["message"] in log_contents]
        assert len(logged) == safe_commit_offer._DROPPED_GROUPS_PREVIEW_COUNT


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
        (agent_dir / "touched.txt").write_text(
            "peer_agent_owned.py\n", encoding="utf-8"
        )
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
    subprocess.run(["git", "add", *filenames], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "landed"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
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
        assert "touched.txt" in lines[0] and "may be incomplete" in lines[0]
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

        assert "touched.txt" in banner and "may be" in banner
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
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init receiver"], cwd=root, check=True)
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

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "memo.send",
            "params": {
                "dry_run": False,
                "topic": "declare-touch-test",
                "to": "example-retrieval-repo-em",
                "title": "Test Memo",
                "body": "This is a test memo body.",
                "kind": "fyi",
                "summary": "Test summary.",
            },
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
# (g) --invoker framing (example-cockpit-repo-em memo, 2026-08-17): a deliberate
# ceremony commit through the mechanical `_default_groups` fallback must not
# be mislabelled as an unattended stop-event rescue. `invoker=None` — the
# undeclared case — must assert nothing about why the commit happened.
# ---------------------------------------------------------------------------


class TestDefaultGroupsInvokerFraming:
    def test_unattended_keeps_stop_event_subject_and_prose(self):
        # Regression lock for the real SessionEnd path — byte-for-byte the
        # original framing.
        groups = safe_commit_offer._default_groups(["a.py"], "sess123456", "unattended")
        assert len(groups) == 1
        subject = groups[0]["message"]
        prose = groups[0]["prose"]
        assert subject == "auto-commit: 1 file(s) rescued at session stop (session sess12, (repo root))"
        assert "Stop-event safety net" in prose
        assert "ended without committing them itself" in prose
        assert "docs/wiki/scoped-safety-commits.md § 3b" in prose

    def test_attended_has_no_stop_event_claim(self):
        groups = safe_commit_offer._default_groups(["a.py"], "sess123456", "attended")
        assert len(groups) == 1
        subject = groups[0]["message"]
        prose = groups[0]["prose"]
        assert subject == "auto-commit: 1 file(s) (session sess12, (repo root))"
        assert "rescued at session stop" not in prose
        assert "ended without committing them itself" not in prose
        assert "deliberate" in prose

    def test_undeclared_invoker_has_no_stop_event_claim(self):
        groups = safe_commit_offer._default_groups(["a.py"], "sess123456", None)
        assert len(groups) == 1
        subject = groups[0]["message"]
        prose = groups[0]["prose"]
        # Subject matches the attended shape (still short/bounded).
        assert subject == "auto-commit: 1 file(s) (session sess12, (repo root))"
        assert "rescued at session stop" not in prose
        assert "ended without committing them itself" not in prose
        # Nor may it claim the opposite (deliberate/ceremony) framing —
        # undeclared means undeclared, not defaulted either way.
        assert "ceremony" not in prose.lower()

    def test_default_invoker_argument_is_none_shaped(self):
        # Calling without the keyword at all reproduces the undeclared shape.
        groups = safe_commit_offer._default_groups(["a.py"], "sess123456")
        assert "rescued at session stop" not in groups[0]["prose"]


class TestGroupsSuppliedPathIgnoresInvoker:
    def test_explicit_groups_path_never_consults_invoker(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        report = safe_commit_offer.auto_commit_session(
            "mine",
            str(repo),
            groups=[{"paths": ["a.py"], "message": "hand-authored subject"}],
            invoker="unattended",
        )
        assert len(report["groups"]) == 1
        assert report["groups"][0]["message"] == "hand-authored subject"
        # Neither invoker-specific framing string leaked into an
        # explicit-groups commit — `_default_groups` was never called.
        assert "rescued at session stop" not in report["groups"][0]["message"]


class TestMainInvokerFlag:
    def test_rejects_unknown_invoker_value(self):
        exit_code = safe_commit_offer.main(["--invoker", "bogus"])
        assert exit_code == 2

    def test_accepts_attended_and_unattended(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        for value in ("attended", "unattended"):
            (repo / ("f_%s.py" % value)).write_text(value)
            scope.touch("mine", "f_%s.py" % value, cwd=str(repo))
            exit_code = safe_commit_offer.main(
                ["--session", "mine", "--root", str(repo), "--invoker", value]
            )
            assert exit_code == 0
        capsys.readouterr()
