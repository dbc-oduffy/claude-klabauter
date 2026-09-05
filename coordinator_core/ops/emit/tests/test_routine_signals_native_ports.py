"""Parity net for C9's routine_signals native ports (count-distill-backlog.sh, the
check-weekly/arch-audit-staleness in-process wiring, and rollups' local-day/review-trail
native sources).

Review: code-reviewer (F1) — ``TestRunStalenessNative`` / ``TestLocalDayAndIsoWeek`` /
``TestReviewTrailFacts`` below close the gap between this docstring's coverage claim and
the file body; previously only the distill-backlog port (``_count_distill_backlog`` /
``_distill_slug`` / ``_resolve_distill_root``) was exercised here despite the docstring
claiming all four native ports.

Purpose: pin each oracle's observable contract with a DETERMINISTIC synthetic fixture,
independent of the live-tree golden fixture used by ``test_emit_parity.py`` (that golden is
a frozen point-in-time capture and drifts against these sites' genuinely time-varying inputs
— git commit counts, wall-clock "today", real archive/wiki content — so it cannot serve as
the observable-contract lock the plan review demanded; see docs/plans/2026-07-21-claude-klabauter-
pure-python-shop-retire-all-bash.md § C9 review note).

Spec backlink: pln-claude-klabauter-pure-python-shop-retire-0f8aee § C9
Port of: count-distill-backlog.sh (DoE 721a71f4, 2026-07-21).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.emit.sections import routine_signals
from coordinator_core.ops.emit.sections.routine_signals import (
    _count_distill_backlog,
    _distill_slug,
    _resolve_coordinator_state_root,
    _resolve_distill_root,
    _run_staleness_native,
)
from coordinator_core.ops.emit.sections.rollups import (
    _iso_week,
    _local_day,
    _review_trail_facts,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestCountDistillBacklogShape:
    """Locks the ``{"pending_count": int, "threshold_days": 30, "computed_state": str}``
    output shape (the exact keys ``count-distill-backlog.sh --format json`` emits via
    ``jq -cn``)."""

    def test_missing_archive_root_raises(self, tmp_path: Path) -> None:
        """No archive/completed dir → RuntimeError (bash oracle: ``exit 1`` + stderr)."""
        with pytest.raises(RuntimeError, match="archive root not found"):
            _count_distill_backlog(tmp_path / "repo", tmp_path / "coordinator")

    def test_empty_archive_dir_is_unknown(self, tmp_path: Path) -> None:
        """archive/completed exists but has zero .md files → computed_state 'unknown'
        (bash:143-146: archive_files_found == 0 → unknown, distinct from 'fresh')."""
        root = tmp_path / ".claude"
        (root / "archive" / "completed").mkdir(parents=True)
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result == {
            "pending_count": 0,
            "threshold_days": 30,
            "computed_state": "unknown",
        }

    def test_all_entries_within_cutoff_is_fresh(self, tmp_path: Path) -> None:
        """Entries exist but none older than the 30-day cutoff → 'fresh', pending_count 0."""
        root = tmp_path / ".claude"
        today = datetime.date.today().isoformat()
        _write(
            root / "archive" / "completed" / "2026-07" / "entry-one.md",
            f"---\ncreated: {today}\nchain: null\n---\nbody\n",
        )
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result["computed_state"] == "fresh"
        assert result["pending_count"] == 0
        assert result["threshold_days"] == 30

    def test_old_entry_not_in_wiki_is_pending(self, tmp_path: Path) -> None:
        """An entry older than cutoff whose slug is absent from the wiki corpus counts as
        pending (bash:97-122 grep -qFl miss)."""
        root = tmp_path / ".claude"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        _write(
            root / "archive" / "completed" / "2026-05" / "2026-05-01-undistilled-a1b2c3.md",
            f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
        )
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result == {
            "pending_count": 1,
            "threshold_days": 30,
            "computed_state": "mild",
        }

    def test_old_entry_present_in_wiki_is_not_pending(self, tmp_path: Path) -> None:
        """Same shape as above, but the slug DOES appear in the wiki corpus → not pending."""
        root = tmp_path / ".claude"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        _write(
            root / "archive" / "completed" / "2026-05" / "2026-05-01-distilled-a1b2c3.md",
            f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
        )
        _write(root / "docs" / "wiki" / "notes.md", "some notes mentioning distilled here\n")
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result == {
            "pending_count": 0,
            "threshold_days": 30,
            "computed_state": "fresh",
        }

    def test_chain_non_null_used_as_slug(self, tmp_path: Path) -> None:
        """chain: <slug> (non-null) overrides filename-derived slug (bash:99-101)."""
        root = tmp_path / ".claude"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        _write(
            root / "archive" / "completed" / "2026-05" / "2026-05-01-whatever-a1b2c3.md",
            f"---\ncreated: {old_day}\nchain: my-real-slug\n---\nbody\n",
        )
        _write(root / "docs" / "wiki" / "notes.md", "references my-real-slug in prose\n")
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result["pending_count"] == 0

    def test_six_or_more_pending_is_stale(self, tmp_path: Path) -> None:
        """pending_count >= 6 → 'stale' band (bash:150-154)."""
        root = tmp_path / ".claude"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        for i in range(6):
            _write(
                root / "archive" / "completed" / "2026-05" / f"2026-05-0{i+1}-x{i}-a1b2c3.md",
                f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
            )
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result["pending_count"] == 6
        assert result["computed_state"] == "stale"

    def test_missing_created_frontmatter_skipped_not_errored(self, tmp_path: Path) -> None:
        """A non-empty .md with no ``created:`` line is a counted-but-skipped legacy
        rollup file (bash: `[[ -z "$created" ]] && continue`) — contributes to
        archive_files_found (so state isn't 'unknown') but not to pending_count."""
        root = tmp_path / ".claude"
        _write(
            root / "archive" / "completed" / "2026-05" / "legacy-rollup.md",
            "no frontmatter here at all\n",
        )
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(root, coordinator_root)
        assert result == {
            "pending_count": 0,
            "threshold_days": 30,
            "computed_state": "fresh",
        }

    def test_archive_scan_is_rooted_at_repo_root_not_coordinator_root(
        self, tmp_path: Path
    ) -> None:
        """The archive/completed scan reads *repo_root* — the emitting repo's own
        archive, parity with the docs/bug-sweep signals beside this one — never the
        coordinator root's install-layout-inferred tree. A populated repo_root archive
        must be counted even when coordinator_root has no archive of its own at all."""
        repo_root = tmp_path / "some-emitting-repo"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        _write(
            repo_root / "archive" / "completed" / "2026-05" / "2026-05-01-undistilled-a1b2c3.md",
            f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
        )
        # A coordinator_root elsewhere entirely, with no archive/completed reachable
        # from it at all (not even via _resolve_distill_root's ladder/fallback).
        coordinator_root = tmp_path / "unrelated-coordinator-checkout"
        coordinator_root.mkdir(parents=True)

        result = _count_distill_backlog(repo_root, coordinator_root)
        assert result["pending_count"] == 1
        assert result["computed_state"] == "mild"


class TestCollectDistillBacklogUsesRepoRoot:
    """``collect()``'s distill-backlog signal must read ``ctx.repo_root``'s own
    archive/completed, matching its ``docs``/``bug-sweep`` siblings in the same
    function — the defect this module exists to close (tc-3 always reported
    pending_count=0/computed_state='unknown' because it scanned ctx.coordinator_root
    instead)."""

    def test_collect_reports_nonzero_pending_from_repo_root_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_root = tmp_path / "emitting-repo"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
        _write(
            repo_root / "archive" / "completed" / "2026-05" / "2026-05-01-undistilled-a1b2c3.md",
            f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
        )
        coordinator_root = tmp_path / "unrelated-coordinator-checkout"
        coordinator_root.mkdir(parents=True)

        monkeypatch.setattr(routine_signals, "_run_staleness_native", lambda *a, **k: "fresh")
        monkeypatch.setattr(routine_signals, "_resolve_coordinator_state_root", lambda root: None)
        monkeypatch.setattr(
            routine_signals,
            "_commits_since_last_batch",
            lambda _root, patterns: {name: 0 for name in patterns},
        )

        ctx = MagicMock()
        ctx.repo_name = "owner/repo"
        ctx.observed_at = "2026-07-22T00:00:00Z"
        ctx.coordinator_root = coordinator_root
        ctx.repo_root = repo_root
        ctx.provenance = lambda *a, **k: {}

        signals, malformed = routine_signals.collect(ctx)
        assert malformed == []
        distill_signal = next(s for s in signals if s["kind"] == "distill-backlog")
        assert distill_signal["computed_state"] == "mild"
        assert distill_signal["inputs"]["pending_count"] == 1


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
class TestCountDistillBacklogUnreadableSubtree:
    """Silent-success guard (state/audits/2026-07-22 audit): a chmod'd dated subdirectory
    must degrade the verdict to 'unknown' — never 'fresh'/'mild' — and must never let the
    caller compute overdue=False from an undercounted pending_count."""

    def test_unreadable_subdir_with_many_pending_entries_is_unknown_not_fresh(
        self, tmp_path: Path
    ) -> None:
        """A readable subdir with zero pending entries plus an UNREADABLE subdir hiding
        >=6 old undistilled files must report computed_state='unknown' (never 'fresh',
        which is what a naive glob-swallowed-PermissionError read would silently report)."""
        root = tmp_path / ".claude"
        old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()

        # A readable, genuinely-clean subdir (nothing pending here).
        readable_dir = root / "archive" / "completed" / "2026-06"
        _write(readable_dir / "fresh-entry.md", f"---\ncreated: {(datetime.date.today()).isoformat()}\nchain: null\n---\nbody\n")

        # A subdir hiding 6 old, undistilled entries — chmod'd unreadable after creation.
        hidden_dir = root / "archive" / "completed" / "2026-05"
        for i in range(6):
            _write(
                hidden_dir / f"2026-05-0{i+1}-hidden-{i}-a1b2c3.md",
                f"---\ncreated: {old_day}\nchain: null\n---\nbody\n",
            )
        coordinator_root = root / "plugins" / "coordinator-claude" / "coordinator"
        coordinator_root.mkdir(parents=True)

        original_mode = hidden_dir.stat().st_mode
        os.chmod(hidden_dir, 0o000)
        try:
            result = _count_distill_backlog(root, coordinator_root)
        finally:
            os.chmod(hidden_dir, original_mode)

        assert result["computed_state"] == "unknown", (
            f"expected 'unknown' on a skipped subtree, got {result['computed_state']!r} — "
            "a naive glob() read would silently see 0 files in the chmod'd dir and could "
            "misreport 'fresh'"
        )
        assert result["computed_state"] != "fresh"
        assert result.get("skipped_subtree_count", 0) >= 1, (
            "expected skipped_subtree_count to record the unreadable subdir"
        )

    def test_routine_signals_collect_overdue_not_false_on_skipped_subtree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """routine_signals.collect()'s distill-backlog signal must not report
        overdue=False when _count_distill_backlog reports a skipped subtree — the
        pending_count in that case is an undercount, not a verified-clean signal."""
        monkeypatch.setattr(
            routine_signals,
            "_count_distill_backlog",
            lambda repo_root, coordinator_root: {
                "pending_count": 0,
                "threshold_days": 30,
                "computed_state": "unknown",
                "skipped_subtree_count": 1,
            },
        )
        monkeypatch.setattr(routine_signals, "_run_staleness_native", lambda *a, **k: "fresh")
        monkeypatch.setattr(routine_signals, "_resolve_coordinator_state_root", lambda root: None)
        monkeypatch.setattr(
            routine_signals,
            "_commits_since_last_batch",
            lambda _root, patterns: {name: 0 for name in patterns},
        )

        ctx = MagicMock()
        ctx.repo_name = "owner/repo"
        ctx.observed_at = "2026-07-22T00:00:00Z"
        ctx.coordinator_root = tmp_path
        ctx.repo_root = tmp_path
        ctx.provenance = lambda *a, **k: {}

        signals, malformed = routine_signals.collect(ctx)
        assert malformed == []
        distill_signal = next(s for s in signals if s["kind"] == "distill-backlog")
        assert distill_signal["overdue"] is True, (
            "distill-backlog overdue must not be False when the underlying scan skipped "
            f"a subtree; got signal={distill_signal!r}"
        )
        assert distill_signal["computed_state"] == "unknown"


class TestDistillSlugDerivation:
    """Locks the filename→slug glob-strip semantics (bash `${base%-??????}` /
    `${no_hash#????-??-??-}`) independent of the full-scan integration tests above."""

    def test_chain_wins_over_filename(self) -> None:
        assert _distill_slug("/x/2026-05-01-foo-a1b2c3.md", "explicit-chain") == "explicit-chain"

    def test_null_chain_falls_back_to_filename_slug(self) -> None:
        assert _distill_slug("/x/2026-05-01-my-slug-a1b2c3.md", "null") == "my-slug"

    def test_empty_chain_falls_back_to_filename_slug(self) -> None:
        assert _distill_slug("/x/2026-05-01-my-slug-a1b2c3.md", "") == "my-slug"


class TestResolveDistillRoot:
    """Locks the two-rung root resolution (bash:19-27): script-location inference,
    else CLAUDE_HOME/~ fallback."""

    def test_script_inferred_root_preferred_when_archive_present(self, tmp_path: Path) -> None:
        claude_home = tmp_path / ".claude"
        (claude_home / "archive" / "completed").mkdir(parents=True)
        coordinator_root = (
            claude_home / "plugins" / "coordinator-claude" / "coordinator"
        )
        coordinator_root.mkdir(parents=True)

        assert _resolve_distill_root(coordinator_root) == claude_home

    def test_falls_back_to_claude_home_env_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        fallback_home = tmp_path / "fallback-home"
        monkeypatch.setenv("CLAUDE_HOME", str(fallback_home))
        # coordinator_root nested somewhere with NO archive/completed 4 levels up.
        coordinator_root = tmp_path / "elsewhere" / "coordinator"
        coordinator_root.mkdir(parents=True)

        assert _resolve_distill_root(coordinator_root) == fallback_home / ".claude"


class TestRunStalenessNative:
    """Locks ``_run_staleness_native``'s degrade-to-"unknown" contract (bash: ``bash "$script"
    2>/dev/null || echo "UNKNOWN"``) — the exception path, the non-zero-exit path, and the
    None-state-root short-circuit, independent of which staleness module (check-weekly /
    check-arch-audit) is wired in."""

    def test_exception_degrades_to_unknown(self, tmp_path: Path) -> None:
        def _boom(argv: list[str]) -> int:
            raise RuntimeError("staleness check exploded")

        assert _run_staleness_native(_boom, str(tmp_path)) == "unknown"

    def test_nonzero_exit_degrades_to_unknown(self, tmp_path: Path) -> None:
        def _fail(argv: list[str]) -> int:
            print("STALE")
            return 1

        assert _run_staleness_native(_fail, str(tmp_path)) == "unknown"

    def test_zero_exit_lowercases_stdout(self, tmp_path: Path) -> None:
        def _ok(argv: list[str]) -> int:
            print("FRESH")
            return 0

        assert _run_staleness_native(_ok, str(tmp_path)) == "fresh"

    def test_none_state_root_short_circuits_without_calling_main_fn(self) -> None:
        """Finding 1 (P1, sliceroutine-signals-ac5-gate-slice2): when the caller could
        not resolve a coordinator state root, this must degrade to "unknown" WITHOUT
        calling main_fn at all — calling it with an empty argv would let the staleness
        module fall through to its own cwd-based _resolve_state_root(), silently
        reintroducing the implicit-cwd dependency AC-5 exists to eliminate."""

        def _explode(argv: list[str]) -> int:
            raise AssertionError("main_fn must not be called when state_root is None")

        assert _run_staleness_native(_explode, None) == "unknown"


class TestResolveCoordinatorStateRoot:
    """Locks ``_resolve_coordinator_state_root``'s four branches (Finding 3, sliceroutine-
    signals-ac5-gate-slice2): meta-repo cwd routes to the engine root/state, sibling-repo cwd
    routes to <git-root>/state, not-a-git-repo returns None, and meta-repo-but-engine-root-
    unresolvable returns None. Mirrors check_weekly_staleness's own
    test_resolve_state_root_* branch coverage, applied to the explicit-cwd variant."""

    def test_not_a_git_repo_returns_none(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(routine_signals, "_cws_git_root", lambda cwd=None: None)
        assert _resolve_coordinator_state_root(tmp_path) is None

    def test_sibling_repo_uses_git_root_state(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(routine_signals, "_cws_git_root", lambda cwd=None: str(tmp_path))
        monkeypatch.setattr(routine_signals, "_cws_claude_home", lambda: "/nonexistent/claude/home")
        assert _resolve_coordinator_state_root(tmp_path) == str(tmp_path / "state")

    def test_meta_repo_routes_to_claude_klabauter_root(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(routine_signals, "_cws_git_root", lambda cwd=None: str(tmp_path))
        monkeypatch.setattr(routine_signals, "_cws_claude_home", lambda: str(tmp_path))
        monkeypatch.setattr(routine_signals, "_cws_claude_klabauter_root", lambda: "/claude-klabauter/root")
        assert _resolve_coordinator_state_root(tmp_path) == str(Path("/claude-klabauter/root") / "state")

    def test_meta_repo_unresolvable_claude_klabauter_returns_none(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(routine_signals, "_cws_git_root", lambda cwd=None: str(tmp_path))
        monkeypatch.setattr(routine_signals, "_cws_claude_home", lambda: str(tmp_path))
        monkeypatch.setattr(routine_signals, "_cws_claude_klabauter_root", lambda: None)
        assert _resolve_coordinator_state_root(tmp_path) is None


class TestCommitsSinceLastBatch:
    """Locks the rebuilt cadence scan (hitlist § G5): ONE spawn for every pattern, a
    depth-capped read of HEAD's own ancestry, and the ``_VERY_STALE`` sentinel where a
    marker is absent. The shape this replaced ran two ``git log`` spawns PER pattern —
    four per emit — the second of them a ``--grep`` over every ref's full history,
    measured at 593.8 ms of process time against 23,402 commits."""

    def _repo(self, tmp_path: Path, subjects: list[str]) -> Path:
        """Build a throwaway repo whose commits carry *subjects*, oldest first."""
        root = tmp_path / "cadence-repo"
        root.mkdir()
        run_git(["-C", str(root), "init", "-q"])
        run_git(["-C", str(root), "config", "user.email", "t@example.invalid"])
        run_git(["-C", str(root), "config", "user.name", "t"])
        for n, subject in enumerate(subjects):
            (root / f"f{n}.txt").write_text(str(n), encoding="utf-8")
            run_git(["-C", str(root), "add", "-A"])
            run_git(["-C", str(root), "commit", "-q", "-m", subject])
        return root

    def test_counts_commits_since_each_marker_in_one_spawn(self, tmp_path, monkeypatch) -> None:
        root = self._repo(tmp_path, ["chore: base", "update-docs run", "feat: a",
                                     "bug-sweep pass", "feat: b", "feat: c"])

        spawns = []
        real = routine_signals.run_git
        monkeypatch.setattr(
            routine_signals, "run_git",
            lambda args, **kw: (spawns.append(list(args)), real(args, **kw))[1],
        )
        result = routine_signals._commits_since_last_batch(
            root, {"docs": "update-docs", "bug": "bug-sweep|bug_sweep"}
        )

        # HEAD is "feat: c"; bug-sweep is 2 back, update-docs 4 back.
        assert result == {"docs": 4, "bug": 2}
        assert len(spawns) == 1, f"one spawn for all patterns, got {len(spawns)}: {spawns}"

    def test_head_itself_matching_counts_zero(self, tmp_path: Path) -> None:
        root = self._repo(tmp_path, ["chore: base", "update-docs run"])
        assert routine_signals._commits_since_last_batch(root, {"docs": "update-docs"}) == {"docs": 0}

    def test_absent_marker_is_the_very_stale_sentinel(self, tmp_path: Path) -> None:
        root = self._repo(tmp_path, ["chore: base", "feat: a"])
        result = routine_signals._commits_since_last_batch(root, {"docs": "update-docs"})
        assert result == {"docs": routine_signals._VERY_STALE}

    def test_marker_beyond_the_depth_cap_reads_very_stale(self, tmp_path, monkeypatch) -> None:
        """Past the cap the honest integer and the sentinel say the same thing — both land
        in the same 'stale / overdue' band, which is all `collect()` branches on."""
        monkeypatch.setattr(routine_signals, "_SCAN_DEPTH", 3)
        root = self._repo(tmp_path, ["update-docs run", "a", "b", "c", "d"])
        result = routine_signals._commits_since_last_batch(root, {"docs": "update-docs"})
        assert result == {"docs": routine_signals._VERY_STALE}

    def test_not_a_git_repo_degrades_every_pattern(self, tmp_path: Path) -> None:
        result = routine_signals._commits_since_last_batch(
            tmp_path, {"docs": "update-docs", "bug": "bug-sweep"}
        )
        assert result == {"docs": routine_signals._VERY_STALE, "bug": routine_signals._VERY_STALE}

    def test_never_asks_git_to_do_the_matching(self, tmp_path, monkeypatch) -> None:
        """`--grep` is what made the walk history-scaled. Matching belongs in Python,
        against a bounded slice — a reader reintroducing `--grep` here reintroduces the
        O(repo history) cost this rebuild removed."""
        root = self._repo(tmp_path, ["update-docs run"])
        seen: list[list[str]] = []
        real = routine_signals.run_git
        monkeypatch.setattr(
            routine_signals, "run_git",
            lambda args, **kw: (seen.append(list(args)), real(args, **kw))[1],
        )
        routine_signals._commits_since_last_batch(root, {"docs": "update-docs"})

        argv = seen[0]
        assert not any(a.startswith("--grep") for a in argv), argv
        assert "--all" not in argv, argv
        assert "-n" in argv and argv[argv.index("-n") + 1] == str(routine_signals._SCAN_DEPTH)


class TestLocalDayAndIsoWeek:
    """Locks rollups' ``_local_day``/``_iso_week`` FORMAT contracts — ``YYYY-MM-DD`` and the
    ISO-week ``YYYY-Www`` string — derived from ``ctx.observed_at``, never the machine's wall
    clock (per docs/plans/2026-09-04-the-weekly-completion-count-means-the-week.md C1: a
    re-emitted historical snapshot must stamp the day/week of the instant it is ABOUT, not the
    day it happens to run on). ``observed_at`` below is deliberately NOT today's date, so these
    assertions fail against a ``date.today()``-based implementation on every day but one."""

    def test_local_day_matches_observed_at_iso(self) -> None:
        ctx = MagicMock()
        ctx.observed_at = "2026-03-17T08:00:00Z"
        assert _local_day(ctx) == "2026-03-17"

    def test_iso_week_matches_observed_at_isocalendar(self) -> None:
        ctx = MagicMock()
        ctx.observed_at = "2026-03-17T08:00:00Z"
        y, w, _ = datetime.date(2026, 3, 17).isocalendar()
        assert _iso_week(ctx) == f"{y}-W{w:02d}"


class TestReviewTrailFacts:
    """Locks ``rollups._review_trail_facts``' valid-set count + verdict group_by (bash:1073-
    1077), sharing the same quarantine filter as ``review_trail.collect`` (Section 3).

    Every call passes an explicit inclusive ``YYYY-MM-DD`` window: the function is
    period-scoped, so there is no unwindowed reading of it to assert. ``_WIDE`` is used
    where the case under test is about quarantine or emptiness rather than the window,
    and is deliberately a real bounded window rather than a "match everything" sentinel —
    a sentinel would let a regression that ignores the bounds pass every test here.
    ``TestReviewTrailFactsPeriodScope`` below owns the bounds themselves."""

    _WIDE = ("2026-01-01", "2026-12-31")

    def _write_record(self, root: Path, name: str, body: dict) -> None:
        path = root / "review-trail" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")

    def test_counts_and_groups_valid_records_by_verdict(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        self._write_record(
            state_root,
            "2026-07-20-101500-sess-a.json",
            {"sha_range": "aaa..bbb", "reviewer": "code-reviewer", "verdict": "OK"},
        )
        self._write_record(
            state_root,
            "2026-07-20-101600-sess-b.json",
            {"sha_range": "ccc..ddd", "reviewer": "code-reviewer", "verdict": "warn"},
        )
        ctx = MagicMock()
        ctx.subprocess_root = state_root
        ctx.central_state_root = state_root

        count, verdicts = _review_trail_facts(ctx, *self._WIDE)

        assert count == 2
        assert verdicts == {"ok": 1, "warn": 1}

    def test_quarantined_record_excluded_from_valid_set(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        self._write_record(
            state_root,
            "2026-07-20-101500-sess-a.json",
            {"sha_range": "aaa..bbb", "reviewer": "code-reviewer", "verdict": "OK"},
        )
        # Missing sha_range — quarantined, excluded from both count and verdicts.
        self._write_record(
            state_root,
            "2026-07-20-101600-sess-b.json",
            {"reviewer": "code-reviewer", "verdict": "warn"},
        )
        ctx = MagicMock()
        ctx.subprocess_root = state_root
        ctx.central_state_root = state_root

        count, verdicts = _review_trail_facts(ctx, *self._WIDE)

        assert count == 1
        assert verdicts == {"ok": 1}

    def test_empty_review_trail_dir_yields_zero(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        ctx = MagicMock()
        ctx.subprocess_root = state_root
        ctx.central_state_root = state_root

        count, verdicts = _review_trail_facts(ctx, *self._WIDE)

        assert count == 0
        assert verdicts == {}


class TestReviewTrailFactsPeriodScope:
    """The week row's review legs count the week, not the lifetime.

    ``_review_trail_facts`` took no window and counted the ENTIRE live+archive trail, so
    a week row published ``chains_completed`` for its ISO week beside
    ``reviews_conducted``/``verdicts`` for all time under one ``period`` label — measured
    on this repo at 35 beside 3167. Same defect class as the completion legs fixed at
    130435f60c, one field over, and a flat contradiction of the ``fact_window`` the row
    now carries.
    """

    def _write_record(self, root: Path, name: str, verdict: str = "OK") -> None:
        path = root / "review-trail" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"sha_range": "aaa..bbb", "reviewer": "code-reviewer", "verdict": verdict}
            ),
            encoding="utf-8",
        )

    def _ctx(self, state_root: Path):
        ctx = MagicMock()
        ctx.subprocess_root = state_root
        ctx.central_state_root = state_root
        return ctx

    def test_records_outside_the_window_are_not_counted(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        self._write_record(state_root, "2026-07-06-101500-in-a.json", "OK")
        self._write_record(state_root, "2026-07-12-101500-in-b.json", "warn")
        # One day before the window opens and one day after it closes — the off-by-one
        # pair, since both bounds are INCLUSIVE.
        self._write_record(state_root, "2026-07-05-101500-out-before.json", "OK")
        self._write_record(state_root, "2026-07-13-101500-out-after.json", "OK")

        count, verdicts = _review_trail_facts(self._ctx(state_root), "2026-07-06", "2026-07-12")

        assert count == 2
        assert verdicts == {"ok": 1, "warn": 1}

    def test_both_bounds_are_inclusive(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        self._write_record(state_root, "2026-07-06-000000-first-day.json", "OK")
        self._write_record(state_root, "2026-07-12-235900-last-day.json", "OK")

        count, verdicts = _review_trail_facts(self._ctx(state_root), "2026-07-06", "2026-07-12")

        assert count == 2
        assert verdicts == {"ok": 2}

    def test_undatable_filename_is_excluded_not_attributed_to_the_window(
        self, tmp_path: Path
    ) -> None:
        """A stem too short to carry a date reads 1970-01-01 — outside every real window.

        Pins the direction of the failure: an unreadable date drops the record from a
        period-scoped count rather than silently crediting it to the current period.
        """
        state_root = tmp_path / "state"
        self._write_record(state_root, "short.json", "OK")

        count, verdicts = _review_trail_facts(self._ctx(state_root), "2026-07-06", "2026-07-12")

        assert count == 0
        assert verdicts == {}

    def test_week_row_facts_and_fact_window_agree(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The emitted row's window and its review legs come from the same two values.

        The regression this forecloses is the two drifting apart again — a row whose
        ``fact_window`` says one thing while its counts were taken over another.
        """
        from coordinator_core.ops.emit.sections import rollups

        state_root = tmp_path / "state"
        self._write_record(state_root, "2026-07-06-101500-in.json", "OK")
        self._write_record(state_root, "2026-06-01-101500-out.json", "blocked")

        ctx = self._ctx(state_root)
        ctx.observed_at = "2026-07-08T00:00:00Z"
        ctx.repo_name = "fixture-repo"
        ctx.provenance = lambda *a, **k: {}

        captured: dict = {}
        real = rollups._review_trail_facts

        def spy(c, start, end):
            captured["window"] = (start, end)
            return real(c, start, end)

        monkeypatch.setattr(rollups, "_review_trail_facts", spy)
        monkeypatch.setattr(rollups, "_query_completions", lambda _c: [])

        recs, _ = rollups.collect(ctx)

        week = next(r for r in recs if r["grain"] == "week")
        assert captured["window"] == (week["fact_window"]["start"], week["fact_window"]["end"])
        # 2026-07-08 is ISO 2026-W28 (Mon 07-06 .. Sun 07-12): the in-window record is
        # counted and the June one is not — the lifetime leg would have returned 2.
        assert week["fact_window"] == {
            "kind": "iso-week",
            "start": "2026-07-06",
            "end": "2026-07-12",
        }
        assert week["deterministic_facts"]["reviews_conducted"] == 1
        assert week["deterministic_facts"]["verdicts"] == {"ok": 1}
