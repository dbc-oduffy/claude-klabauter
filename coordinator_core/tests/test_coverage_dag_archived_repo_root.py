"""
coordinator_core.tests.test_coverage_dag_archived_repo_root — Regression tests for the
archived-handoff repo_root mis-resolution bug in dag.walk_forward / resolve_target.

Bug: dag.walk_forward called without an explicit repo_root self-infers it two-dirs-up
from the start node's own directory (_repo_root_from_handoff_dir). When the start node
is a month-nested archived handoff (`<root>/archive/handoffs/YYYY-MM/<name>.md`), two-up
yields `<root>/archive` — WRONG — which poisons repo_root for the entire ancestor walk:
every predecessor edge fails to resolve and the DAG collapses (coverage.py's
_derive_dag_chain_set then reports INDETERMINATE).

Coverage:
  (a) Case A — month-nested start, bare-basename predecessor ref, explicit repo_root
      passed → walk resolves the live ancestor.
  (b) Case B — same, but repo-relative predecessor ref (`state/handoffs/<name>.md`) →
      walk resolves the live ancestor.
  (c) Case C — regression guard: the SAME month-nested start, WITHOUT an explicit
      repo_root (the old buggy call shape) → the ancestor is NOT resolved, demonstrating
      exactly what passing repo_root explicitly repairs.
  (d) resolve_target direct unit case — both ref conventions resolve to the live
      ancestor's absolute path when repo_root is passed explicitly.

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-coverage-gate-archived-handoff-dag-mis-resolves-repo-root.md
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core import dag


# ---------------------------------------------------------------------------
# Fixture: clear dag._FRONTMATTER_CACHE between tests (mirrors test_dag_edge_kinds.py
# convention — module-level cache state must not leak between test cases).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """Clear dag._FRONTMATTER_CACHE before and after each test."""
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_handoff(path: Path, *, slug: str, status: str = "active",
                    predecessor: str = "none", **extra_fields: str) -> None:
    """Write a minimal handoff file at path.

    Extra keyword arguments are written as additional frontmatter fields.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_lines = "".join(f"{k}: {v}\n" for k, v in extra_fields.items())
    path.write_text(
        f"---\n"
        f"slug: {slug}\n"
        f"status: {status}\n"
        f"predecessor: {predecessor}\n"
        f"{extra_lines}"
        f"---\n"
        f"# Handoff body\n"
    )


def _build_layout(tmp_path: Path, *, predecessor_ref: str) -> tuple:
    """Build the standard repo layout used by all cases in this module.

    Returns (root, closing_path, liveanc_path).
    """
    root = tmp_path
    live_dir = root / "state" / "handoffs"
    archive_month_dir = root / "archive" / "handoffs" / "2026-07"

    liveanc_path = live_dir / "liveanc.md"
    _write_handoff(liveanc_path, slug="liveanc", predecessor="none")

    closing_path = archive_month_dir / "closing.md"
    _write_handoff(closing_path, slug="closing", predecessor=predecessor_ref)

    return root, closing_path, liveanc_path


# ---------------------------------------------------------------------------
# (a) Case A — month-nested start, bare-basename ref, explicit repo_root
# ---------------------------------------------------------------------------

class TestWalkForwardArchivedStartExplicitRepoRoot:
    def test_bare_basename_ref_resolves_live_ancestor(self, tmp_path):
        """Month-nested start + bare-basename predecessor ref + explicit repo_root
        resolves both the closing node and the live ancestor."""
        root, closing_path, liveanc_path = _build_layout(
            tmp_path, predecessor_ref="liveanc.md"
        )

        result = dag.walk_forward(
            str(closing_path),
            edge_kinds={"predecessor", "additional_predecessors"},
            repo_root=str(root),
        )

        abs_liveanc = os.path.abspath(str(liveanc_path))
        assert abs_liveanc in result["nodes"], (
            "explicit repo_root should let the walk resolve the bare-basename "
            f"predecessor ref from a month-nested start; nodes={list(result['nodes'])}"
        )
        assert result["terminatedEarly"] == "", (
            "expected a clean walk (no missing-link) with explicit repo_root, got "
            f"terminatedEarly={result['terminatedEarly']!r}"
        )

    def test_repo_relative_ref_resolves_live_ancestor(self, tmp_path):
        """Month-nested start + repo-relative predecessor ref
        (`state/handoffs/liveanc.md`) + explicit repo_root resolves the live ancestor."""
        root, closing_path, liveanc_path = _build_layout(
            tmp_path, predecessor_ref="state/handoffs/liveanc.md"
        )

        result = dag.walk_forward(
            str(closing_path),
            edge_kinds={"predecessor", "additional_predecessors"},
            repo_root=str(root),
        )

        abs_liveanc = os.path.abspath(str(liveanc_path))
        assert abs_liveanc in result["nodes"], (
            "explicit repo_root should let the walk resolve the repo-relative "
            f"predecessor ref from a month-nested start; nodes={list(result['nodes'])}"
        )
        assert result["terminatedEarly"] == "", (
            "expected a clean walk (no missing-link) with explicit repo_root, got "
            f"terminatedEarly={result['terminatedEarly']!r}"
        )


# ---------------------------------------------------------------------------
# (c) Case C — regression guard: the OLD bug (no explicit repo_root)
# ---------------------------------------------------------------------------

class TestWalkForwardArchivedStartInferredRepoRootRegression:
    def test_inferred_repo_root_fails_to_resolve_live_ancestor(self, tmp_path):
        """Regression guard — without an explicit repo_root, walk_forward infers
        repo_root two-dirs-up from the month-nested archive dir (WRONG: yields
        <root>/archive, not <root>), so the live ancestor is NOT resolved. This is
        exactly the bug that passing repo_root explicitly (coverage.py Step 1) fixes."""
        root, closing_path, liveanc_path = _build_layout(
            tmp_path, predecessor_ref="liveanc.md"
        )

        result = dag.walk_forward(
            str(closing_path),
            edge_kinds={"predecessor", "additional_predecessors"},
            # No repo_root passed — reproduces the pre-fix call shape.
        )

        abs_liveanc = os.path.abspath(str(liveanc_path))
        if abs_liveanc in result["nodes"]:
            # If the inferred-root path happens to still resolve (e.g. environment
            # quirk), don't hard-fail — but this would mean the regression guard no
            # longer demonstrates the divergence this test exists to document.
            pytest.fail(
                "expected the inferred-repo_root (no explicit repo_root) call to FAIL "
                "to resolve the live ancestor from a month-nested start — this is the "
                "exact divergence that motivates passing repo_root explicitly; got "
                f"nodes={list(result['nodes'])}"
            )
        assert result["terminatedEarly"] == "missing-link", (
            "expected 'missing-link' when repo_root is inferred from a month-nested "
            f"archive start, got terminatedEarly={result['terminatedEarly']!r}"
        )


# ---------------------------------------------------------------------------
# (d) resolve_target direct unit case
# ---------------------------------------------------------------------------

class TestResolveTargetRootAnchoredLiveResolution:
    def test_bare_basename_and_repo_relative_both_resolve(self, tmp_path):
        """resolve_target, called with handoff_dir set to the month-nested archive
        dir and repo_root set to the true root, resolves BOTH ref conventions
        (bare basename and repo-relative) to the live ancestor's absolute path."""
        root, _closing_path, liveanc_path = _build_layout(
            tmp_path, predecessor_ref="liveanc.md"
        )
        handoff_dir = str(root / "archive" / "handoffs" / "2026-07")
        abs_liveanc = os.path.abspath(str(liveanc_path))

        resolved_basename = dag.resolve_target("liveanc.md", handoff_dir, str(root))
        assert resolved_basename == abs_liveanc, (
            f"resolve_target(bare basename) expected {abs_liveanc!r}, got "
            f"{resolved_basename!r}"
        )

        resolved_relative = dag.resolve_target(
            "state/handoffs/liveanc.md", handoff_dir, str(root)
        )
        assert resolved_relative == abs_liveanc, (
            f"resolve_target(repo-relative) expected {abs_liveanc!r}, got "
            f"{resolved_relative!r}"
        )


# ---------------------------------------------------------------------------
# (e) The premise that licensed deleting the archival live-children guard.
#
# 2026-08-28: the guard's last surviving arm kept a live `forked_from` child
# (a spinoff) blocking archival, on the stated ground that archiving would
# "strand that spinoff's own origin pointer (DR-224, AC4)". That citation does
# not resolve — DR-224 contains no AC4, and its actual contract makes
# has-children mean SUPERSEDE. The guard was deleted, but a guard whose stated
# reason is false may still be load-bearing for an unstated one, so the
# premise was MEASURED rather than argued. These tests pin that measurement so
# a future reader can see what the deletion rests on instead of taking it on
# the same trust the original claim asked for.
# ---------------------------------------------------------------------------


class TestSpinoffOriginSurvivesArchivalOfItsOrigin:
    def test_forked_from_resolves_before_and_after_the_origin_is_archived(
        self, tmp_path
    ):
        """A spinoff's `forked_from` pointer resolves to its origin whether the
        origin sits in state/handoffs/ or archive/handoffs/.

        This is the exact move the archival path makes (a git mv between those
        two trees), so if archiving stranded the pointer it would fail here.
        """
        root = tmp_path
        live_dir = root / "state" / "handoffs"
        archive_month = root / "archive" / "handoffs" / "2026-08"

        origin = live_dir / "origin.md"
        _write_handoff(origin, slug="origin", predecessor="none")
        # Schema rule A3a-3 forces a spinoff's `predecessor` to none; the
        # forked_from edge is the ONLY way back, which is precisely why the
        # stranding claim would have mattered had it been true.
        _write_handoff(
            live_dir / "spin.md", slug="spin", predecessor="none",
            forked_from="origin.md",
        )

        before = dag.resolve_target(
            "origin.md", str(live_dir), str(root), id_index={},
            include_history_tier=False,
        )
        assert before and before != "git-history"
        assert Path(before).name == "origin.md"
        assert "state" in Path(before).parts

        archive_month.mkdir(parents=True, exist_ok=True)
        origin.rename(archive_month / "origin.md")
        dag._FRONTMATTER_CACHE.clear()

        after = dag.resolve_target(
            "origin.md", str(live_dir), str(root), id_index={},
            include_history_tier=False,
        )
        assert after and after != "git-history", (
            "archiving the origin stranded the spinoff's forked_from pointer — "
            "the premise the 2026-08-28 guard deletion rests on has broken; "
            "re-open that decision before doing anything else"
        )
        assert Path(after).name == "origin.md"
        assert "archive" in Path(after).parts

    def test_the_resolver_still_needs_an_explicit_repo_root(self, tmp_path):
        """The honest limit on the test above, pinned so it is not overread.

        Resolution survives archival only for callers that pass `repo_root`
        explicitly. A caller that self-infers it two-dirs-up from an archived
        node's own directory gets `<root>/archive` and resolves nothing — case
        (c) in this module is that bug, and it predates the guard deletion.

        Deleting the guard did not create that hazard; it increases how many
        records are archived and therefore how often the hazard is reachable.
        Any consumer added here must pass repo_root explicitly.
        """
        root = tmp_path
        archive_month = root / "archive" / "handoffs" / "2026-08"
        _write_handoff(root / "state" / "handoffs" / "origin.md",
                       slug="origin", predecessor="none")
        archived_spin = archive_month / "spin.md"
        _write_handoff(archived_spin, slug="spin", predecessor="none",
                       forked_from="origin.md")

        wrong_root = archived_spin.parent.parent  # the two-up self-inference
        resolved = dag.resolve_target(
            "origin.md", str(archived_spin.parent), str(wrong_root),
            id_index={}, include_history_tier=False,
        )
        assert not resolved or resolved == "git-history", (
            "self-inferred repo_root unexpectedly resolved — if this now works, "
            "case (c) above is stale and this caveat can be dropped"
        )
