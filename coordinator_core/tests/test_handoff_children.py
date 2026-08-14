"""
coordinator_core.tests.test_handoff_children — Regression tests for the
"handoff.has_live_children" op, focusing on the common_dir→worktree_root
derivation bug fixed in ops/handoff_children.py.

Root cause: the router keys handoff.has_live_children on "common_dir"
(ipc.py _OP_KEY_SCOPE), passing repo_root = <worktree>/.git.  Before the fix,
_collect_handoff_paths received that raw git common dir and resolved
.git/state/handoffs/ — a path that never exists → always empty → exit_code=2
(indeterminate) for every call regardless of actual handoff state.

Fix: _handoff_has_live_children now calls main_worktree_root(effective_repo_root)
before _collect_handoff_paths, mapping the common_dir to the worktree root so the
correct state/handoffs/ and archive/handoffs/ subtrees are scanned.

Tests:
    - test_live_child_detected: candidate with one child handoff → exit_code=0,
      referenced=True.  Invoked with repo_root=<tmpdir>/.git (the common_dir shape
      the router supplies) to reproduce the exact failure mode.
    - test_no_live_children: candidate with no children → exit_code=1,
      referenced=False.

All handlers are async; asyncio.run() is used to avoid the pytest-asyncio
dependency (mirrors test_commit_anchors.py convention).

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C4
Bug-fix backlink: coordinator_core/ops/handoff_children.py (common_dir→worktree_root)
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed.

    Some handlers exercised here are plain ``def`` (no ``await`` in their
    body) and already resolve to a plain value by the time they reach here;
    pass those through unchanged.
    """
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


# ---------------------------------------------------------------------------
# Minimal ServiceContext stub
# ---------------------------------------------------------------------------

class _FakeCtx:
    """Minimal ServiceContext stand-in sufficient for handoff.has_live_children.

    The op only reads ctx.repo_root as a fallback when repo_root kwarg is None.
    We always supply repo_root explicitly (simulating the router), so ctx.repo_root
    is None here — matching the global-multiplex topology where ctx carries no
    founding root.
    """
    repo_root = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """Return a tmp directory shaped like a git worktree root.

    Creates:
      <tmp_path>/.git/           — the git common dir (what the router passes as repo_root)
      <tmp_path>/state/handoffs/ — live handoff scan subtree

    Note: this is NOT a real git repo (no git init).  The op only performs
    filesystem scans — it does not invoke git — so a real repo is unnecessary.
    The .git dir is created to simulate the common_dir path the router supplies.
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Test: live child present → exit_code=0, referenced=True
# ---------------------------------------------------------------------------

class TestLiveChildDetected:
    """Router supplies repo_root=<worktree>/.git; fix maps to worktree root correctly."""

    def test_exit_code_0_when_child_references_candidate(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        handoff_dir = worktree / "state" / "handoffs"

        # Write the candidate handoff (the one we're testing)
        candidate = handoff_dir / "candidate-handoff.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-001
                goal: Build the widget
                ---
                Candidate handoff body.
            """)
        )

        # Write the child handoff — its frontmatter names the candidate via predecessor:
        child = handoff_dir / "child-handoff.md"
        child.write_text(
            textwrap.dedent(f"""\
                ---
                session_id: session-002
                goal: Continue the widget
                predecessor: {candidate}
                ---
                Child handoff body.
            """)
        )

        # Simulate the router: pass repo_root = <worktree>/.git (the common_dir)
        common_dir = worktree / ".git"

        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        )

        assert result["exit_code"] == 0, (
            f"expected exit_code=0 (has live children), got {result}"
        )
        assert result.get("referenced") is True, (
            f"expected referenced=True, got {result}"
        )
        assert any(
            "child-handoff.md" in c for c in result.get("children", [])
        ), f"child-handoff.md not in children: {result.get('children')}"

    def test_pre_fix_collect_paths_returns_empty_for_git_dir(self, worktree: Path) -> None:
        """Genuine pre-fix simulation: _collect_handoff_paths given the raw git common
        dir returns [] — the pre-fix failure mode that caused every call to return
        exit_code=2 (indeterminate) regardless of actual handoff state.

        Pre-fix: _collect_handoff_paths received <worktree>/.git directly and
        scanned <worktree>/.git/state/handoffs/ — which does not exist → live_paths
        empty → indeterminate exit_code=2.

        Review: code-reviewer (F6) — prior version only asserted the wrong path does
        not exist (fixture-setup sanity check), not that the function returns [].
        This exercises the actual pre-fix behavior directly.
        """
        from coordinator_core.ops.handoff_children import _collect_handoff_paths

        # Passing the git common dir (not the worktree root) must return empty —
        # <worktree>/.git/state/handoffs/ does not exist.
        paths, scan_errors = _collect_handoff_paths(worktree / ".git")
        assert paths == [], (
            f"expected [] when _collect_handoff_paths given raw git common dir; got: {paths}"
        )
        assert scan_errors == [], (
            "neither subtree exists under <worktree>/.git — an absent subtree is NOT a "
            f"scan error (only an unreadable-but-present one is); got: {scan_errors}"
        )


# ---------------------------------------------------------------------------
# Test: no live children → exit_code=1, referenced=False
# ---------------------------------------------------------------------------

class TestNoLiveChildren:
    """Candidate with no children → safe-to-archive verdict."""

    def test_exit_code_1_when_no_child_references_candidate(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        handoff_dir = worktree / "state" / "handoffs"

        # Candidate handoff
        candidate = handoff_dir / "lone-candidate.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-010
                goal: Lone task
                ---
                Candidate body — no child references this.
            """)
        )

        # A sibling handoff that references a DIFFERENT predecessor (not the candidate)
        sibling = handoff_dir / "sibling-handoff.md"
        sibling.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-011
                goal: Unrelated task
                predecessor: /some/other/handoff.md
                ---
                Sibling body.
            """)
        )

        common_dir = worktree / ".git"

        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        )

        assert result["exit_code"] == 1, (
            f"expected exit_code=1 (no live children), got {result}"
        )
        assert result.get("referenced") is False, (
            f"expected referenced=False, got {result}"
        )
        assert result.get("children", []) == [], (
            f"expected empty children list, got {result.get('children')}"
        )


# ---------------------------------------------------------------------------
# Test: archive/handoffs subtree exercised (Finding 4)
# ---------------------------------------------------------------------------

class TestArchiveSubtree:
    """Cross-subtree cases: candidate in archive, child in state (and vice versa).

    Review: code-reviewer (F4) — archive/handoffs/ subtree was never exercised by any
    test; a regression in the archive scan would be invisible.
    """

    def test_candidate_in_archive_child_in_state(self, worktree: Path) -> None:
        """Candidate archived; child lives in state/handoffs/ and still references it.

        This is the primary archival-safety scenario: the guard must NOT incorrectly
        approve archival of a candidate that a live child still references.
        """
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        archive_dir = worktree / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        state_dir = worktree / "state" / "handoffs"

        # Candidate lives in archive/
        candidate = archive_dir / "archived-candidate.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-arc-001
                goal: Archived candidate
                ---
                Archived candidate body.
            """)
        )

        # Child lives in state/ and references the archived candidate
        child = state_dir / "live-child-of-archived.md"
        child.write_text(
            textwrap.dedent(f"""\
                ---
                session_id: session-arc-002
                goal: Live child
                predecessor: {candidate}
                ---
                Live child body.
            """)
        )

        common_dir = worktree / ".git"
        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        )

        assert result["exit_code"] == 0, (
            f"expected exit_code=0 (archived candidate still has live children), got {result}"
        )
        assert result.get("referenced") is True, (
            f"expected referenced=True, got {result}"
        )

    def test_child_in_archive_references_live_candidate(self, worktree: Path) -> None:
        """Child is archived and references a live candidate.

        Confirms an archive-resident child, though still scanned by rglob, is
        excluded from the live-children set (archive-residency exclusion) →
        candidate is safe to archive.
        """
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        archive_dir = worktree / "archive" / "handoffs" / "2026-06"
        archive_dir.mkdir(parents=True)
        state_dir = worktree / "state" / "handoffs"

        # Candidate lives in state/
        candidate = state_dir / "live-candidate.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-arc-010
                goal: Live candidate
                ---
                Live candidate body.
            """)
        )

        # Child is in archive/ (month-foldered subdirectory) and references candidate
        child = archive_dir / "archived-child.md"
        child.write_text(
            textwrap.dedent(f"""\
                ---
                session_id: session-arc-011
                goal: Archived child
                predecessor: {candidate}
                ---
                Archived child body.
            """)
        )

        common_dir = worktree / ".git"
        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        )

        assert result["exit_code"] == 1, (
            f"expected exit_code=1 (archive-resident child is excluded from the "
            f"live set → safe to archive), got {result}"
        )
        assert result.get("referenced") is False, (
            f"expected referenced=False (archive-resident child is excluded from "
            f"the live set → safe to archive), got {result}"
        )


# ---------------------------------------------------------------------------
# Test: fail-closed indeterminate path (Finding 5)
# ---------------------------------------------------------------------------

class TestIndeterminate:
    """Both repo roots None → fail-closed exit_code=2.

    Review: code-reviewer (F5) — the indeterminate branch was entirely untested.
    For a data-loss-adjacent archival-safety guard, the fail-closed path is load-bearing.
    """

    def test_indeterminate_when_no_repo_root(self, tmp_path: Path) -> None:
        """When repo_root kwarg and ctx.repo_root are both None, the handler returns
        exit_code=2 (fail-closed) and omits the `referenced` key.
        """
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        # Minimal candidate file — validation passes before the repo_root check
        candidate = tmp_path / "orphan-candidate.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-ind-001
                goal: Orphan candidate
                ---
                Orphan body.
            """)
        )

        # Both roots None — simulates a routing failure (no _origin_worktree in request,
        # no founding repo_root in ctx)
        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=None,
            )
        )

        assert result["exit_code"] == 2, (
            f"expected exit_code=2 (fail-closed indeterminate), got {result}"
        )
        assert "referenced" not in result, (
            f"referenced key must be absent on indeterminate to avoid false 'safe-to-archive' read; got {result}"
        )


# ---------------------------------------------------------------------------
# Test: repo_root=None fail-closed on initiatives_serve and roadmap_serve (Finding 4)
# ---------------------------------------------------------------------------
#
# Review: code-reviewer — W3 removed TestCtxRepoRootFallback (correctly — it tested the
# removed ctx fallback), but the repo_root=None → fail-closed invariant on all three ops
# (handoff_children, initiatives_serve, roadmap_serve) now had no test for the latter two.
# handoff_children is covered by TestIndeterminate above. This section covers the other two.

class TestInitiativesServeFailClosed:
    """initiatives_serve returns empty set when repo_root=None."""

    def test_empty_set_when_no_repo_root(self) -> None:
        """initiative.serve_set returns {initiatives: []} when repo_root=None
        (fail-safe: empty is safe for a dropdown, no raise).
        """
        from coordinator_core.ops.initiatives_serve import _handler as _initiatives_handler

        result = _initiatives_handler(params={}, repo_root=None)

        assert result == {"initiatives": []}, (
            f"expected {{'initiatives': []}} when repo_root=None; got {result}"
        )


class TestRoadmapServeFailClosed:
    """roadmap_serve returns empty well-formed payload when repo_root=None."""

    def test_empty_payload_when_no_repo_root(self) -> None:
        """roadmap.serve returns empty well-formed payload when repo_root=None
        (fail-safe: unknown worktree is not a 500; returns zero-node guard shape).
        """
        from coordinator_core.ops.roadmap_serve import _handler as _roadmap_handler

        result = _run(_roadmap_handler(params={"roadmap_id": "test-rid"}, repo_root=None))

        # Exact shape from roadmap_serve.py repo_root=None branch (lines 110-115)
        assert result.get("nodes") == [], (
            f"expected empty nodes list when repo_root=None; got {result}"
        )
        assert result.get("edges") == [], (
            f"expected empty edges list when repo_root=None; got {result}"
        )
        assert result.get("critical_path") == [], (
            f"expected empty critical_path when repo_root=None; got {result}"
        )
        roll_up = result.get("roll_up", {})
        assert roll_up.get("total") == 0, (
            f"expected roll_up.total=0 when repo_root=None; got roll_up={roll_up}"
        )


# ---------------------------------------------------------------------------
# Test: unscannable subtree — silent-success guard (state/audits/2026-07-22
#     silent-success audit). An unreadable state/handoffs/ or archive/handoffs/
#     dir must fail closed (exit_code=2), never silently read as "candidate
#     has no children" (which would falsely green-light an archive of a
#     candidate whose live child is sitting under the unreadable subtree).
# ---------------------------------------------------------------------------

_SKIP_CHMOD = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)


class TestUnscannableSubtreeFailsClosed:
    """Permission-denied state/handoffs/ or archive/handoffs/ must yield
    exit_code=2 (indeterminate/fail-closed), distinguishable from the genuine
    "no live children" exit_code=1 verdict."""

    @_SKIP_CHMOD
    def test_unreadable_state_handoffs_dir_fails_closed(self, worktree: Path) -> None:
        """A live child sitting inside an unreadable state/handoffs/ dir must not
        be silently missed — the whole op fails closed rather than risk a false
        "safe to archive" verdict.

        The candidate itself is seeded under archive/handoffs/ (kept readable)
        so the path-containment/existence checks that run BEFORE the enumeration
        scan can still resolve it — isolating the assertion to the state/handoffs/
        scan failure specifically, rather than an earlier "candidate not found"
        short-circuit that would also happen to return exit_code=2 for the wrong
        reason.
        """
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        archive_dir = worktree / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        candidate = archive_dir / "candidate-in-archive.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-unreadable-001
                goal: Candidate under unreadable state dir
                ---
                Candidate body.
            """)
        )

        # A live child that references the candidate, hidden inside the
        # unreadable state/handoffs/ dir — if that dir were silently skipped
        # (pre-fix `except OSError: pass`), this child would go undetected and
        # the candidate would falsely read as safe-to-archive.
        handoff_dir = worktree / "state" / "handoffs"
        child = handoff_dir / "child-of-unreadable-state.md"
        child.write_text(
            textwrap.dedent(f"""\
                ---
                session_id: session-unreadable-002
                goal: Live child
                predecessor: {candidate}
                ---
                Child body.
            """)
        )

        common_dir = worktree / ".git"
        original_mode = handoff_dir.stat().st_mode
        os.chmod(handoff_dir, 0o000)
        try:
            result = _run(
                _handoff_has_live_children(
                    params={"candidate": str(candidate)},
                    repo_root=common_dir,
                )
            )
        finally:
            os.chmod(handoff_dir, original_mode)

        assert result["exit_code"] == 2, (
            f"expected exit_code=2 (fail-closed) when state/handoffs/ is unreadable; "
            f"got {result}"
        )
        assert "referenced" not in result, (
            f"referenced key must be absent on the fail-closed path; got {result}"
        )
        assert "error" in result and result["error"], result

    @_SKIP_CHMOD
    def test_unreadable_archive_handoffs_dir_fails_closed(self, worktree: Path) -> None:
        """Same fail-closed guard, applied to an unreadable archive/handoffs/
        subtree — mirrors the state/handoffs/ case above."""
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        candidate = worktree / "state" / "handoffs" / "candidate-archive-unreadable.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-unreadable-archive-001
                goal: Candidate with archived child hidden by an unreadable dir
                ---
                Candidate body.
            """)
        )

        archive_dir = worktree / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        child = archive_dir / "archived-child-of-unreadable.md"
        child.write_text(
            textwrap.dedent(f"""\
                ---
                session_id: session-unreadable-archive-002
                goal: Archived child
                predecessor: {candidate}
                ---
                Archived child body.
            """)
        )

        common_dir = worktree / ".git"
        original_mode = archive_dir.stat().st_mode
        os.chmod(archive_dir, 0o000)
        try:
            result = _run(
                _handoff_has_live_children(
                    params={"candidate": str(candidate)},
                    repo_root=common_dir,
                )
            )
        finally:
            os.chmod(archive_dir, original_mode)

        assert result["exit_code"] == 2, (
            f"expected exit_code=2 (fail-closed) when archive/handoffs/ is unreadable; "
            f"got {result}"
        )
        assert "referenced" not in result, (
            f"referenced key must be absent on the fail-closed path; got {result}"
        )
        assert "error" in result and result["error"], result

    @_SKIP_CHMOD
    def test_readable_tree_still_yields_definite_verdict(self, worktree: Path) -> None:
        """Baseline/contrast: the SAME shape with a fully-readable tree still
        resolves to a definite exit_code (0 or 1), never exit_code=2 — proving
        the fail-closed path above is specific to the unreadable subtree, not a
        general regression."""
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        handoff_dir = worktree / "state" / "handoffs"
        candidate = handoff_dir / "candidate-readable.md"
        candidate.write_text(
            textwrap.dedent("""\
                ---
                session_id: session-readable-001
                goal: Candidate, fully readable tree
                ---
                Candidate body.
            """)
        )

        common_dir = worktree / ".git"
        result = _run(
            _handoff_has_live_children(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        )

        assert result["exit_code"] in (0, 1), (
            f"expected a definite verdict (0 or 1) over a fully-readable tree; got {result}"
        )


# ---------------------------------------------------------------------------
# C6b — regression tests for PIN-1's `blocked_by_dependents` resolver
# (coordinator_core/ops/handoff_children.py, authored by a peer chunk, C1).
#
# RED-BEFORE-GREEN: `blocked_by_dependents` does not exist on disk yet at the
# time these tests were authored. Every test below imports it function-locally
# (not at module scope) so that only THESE tests go red with ImportError, not
# the whole file's collection — the pre-existing tests above, and the
# `_DEFAULT_EDGE_KINDS` pin test below, must keep passing untouched.
#
# Spec: docs/plans/2026-08-02-roadmap-baton-supersession-hazard.md § PIN-1
# (chunk C6b's own dispatch brief).
# ---------------------------------------------------------------------------


def _write_handoff_fm(path: Path, fields: "dict[str, object]", body: str = "Body.\n") -> Path:
    """Write a minimal handoff markdown file with arbitrary frontmatter fields.

    `fields` values that are lists are rendered as a YAML flow sequence (e.g.
    `blocked_by: [foo, bar]`) — sufficient for this test's frontmatter shapes,
    not a general YAML emitter.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _assert_five_key_shape(result: "dict[str, object]") -> None:
    """PIN-1 item 6: the five-key return shape is present on EVERY outcome."""
    for key in ("state", "dependents", "identifiers", "scan_errors", "error"):
        assert key in result, f"blocked_by_dependents result missing key {key!r}: {result!r}"
    assert result["state"] in ("dependents", "none", "indeterminate"), (
        f"state must be one of the tri-state values; got {result['state']!r}"
    )


class TestBlockedByDependents:
    """PIN-1: `blocked_by_dependents(candidate_path, worktree_root, exclude=None)`.

    Which LIVE handoffs list this candidate's stub id in their `blocked_by`?
    """

    def test_live_referrer_via_blocked_by_yields_dependents(self, worktree: Path) -> None:
        """Item 1: a candidate with one live handoff listing its stub id in
        `blocked_by` -> state=="dependents", that path in `dependents`."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "candidate.md",
            {"stub_id": "cand-01", "status": "open"},
        )
        referrer = _write_handoff_fm(
            handoff_dir / "referrer.md",
            {"stub_id": "ref-01", "status": "open", "blocked_by": ["cand-01"]},
        )

        result = blocked_by_dependents(candidate, worktree)

        _assert_five_key_shape(result)
        assert result["state"] == "dependents", (
            f"expected state=='dependents' for a live blocked_by referrer; got {result!r}"
        )
        assert str(referrer.resolve()) in result["dependents"], (
            f"referrer path not in dependents: {result['dependents']!r}"
        )
        assert result["scan_errors"] == []
        assert result["error"] is None

    def test_only_terminal_referrer_yields_none(self, worktree: Path) -> None:
        """Item 2: a candidate whose only `blocked_by` referrer is terminal
        (per `_is_terminal_or_archived_child`) -> state=="none"."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "candidate2.md",
            {"stub_id": "cand-02", "status": "open"},
        )
        _write_handoff_fm(
            handoff_dir / "terminal-referrer.md",
            {
                "stub_id": "ref-02",
                "status": "superseded",
                "blocked_by": ["cand-02"],
            },
        )

        result = blocked_by_dependents(candidate, worktree)

        _assert_five_key_shape(result)
        assert result["state"] == "none", (
            f"a terminal-status-only referrer must not count as a live "
            f"dependent; expected state=='none', got {result!r}"
        )
        assert result["dependents"] == []

    def test_only_open_status_closed_deployment_state_referrer_yields_none(
        self, worktree: Path
    ) -> None:
        """DR-084 regression: a candidate whose only `blocked_by` referrer
        carries `status: open` + a terminal `deployment_state: closed` (the
        close-handoff verb's shape — status stays open, only deployment_state
        is stamped terminal) must not count as a live dependent -> state==
        "none". Sibling to test_only_terminal_referrer_yields_none, at the
        deployment_state axis rather than the status axis (per
        `_is_terminal_or_archived_child`'s DR-084 terminal-deployment-state
        rule)."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "candidate2b.md",
            {"stub_id": "cand-02b", "status": "open"},
        )
        _write_handoff_fm(
            handoff_dir / "closed-deployment-referrer.md",
            {
                "stub_id": "ref-02b",
                "status": "open",
                "deployment_state": "closed",
                "blocked_by": ["cand-02b"],
            },
        )

        result = blocked_by_dependents(candidate, worktree)

        _assert_five_key_shape(result)
        assert result["state"] == "none", (
            f"a status:open + deployment_state:closed-only referrer must not "
            f"count as a live dependent; expected state=='none', got {result!r}"
        )
        assert result["dependents"] == []

    @_SKIP_CHMOD
    def test_scan_errors_yield_indeterminate_not_none(self, worktree: Path) -> None:
        """Item 3 (THE TRI-STATE CASE THAT MATTERS): non-empty scan_errors ->
        state=="indeterminate", NOT "none". Conflating the two is the exact
        failure this guard exists to prevent."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        # Candidate kept readable (under archive/handoffs/) so identifier
        # resolution succeeds before the scan of the unreadable subtree is hit.
        archive_dir = worktree / "archive" / "handoffs"
        candidate = _write_handoff_fm(
            archive_dir / "candidate3.md",
            {"stub_id": "cand-03", "status": "consumed"},
        )

        handoff_dir = worktree / "state" / "handoffs"
        original_mode = handoff_dir.stat().st_mode
        os.chmod(handoff_dir, 0o000)
        try:
            result = blocked_by_dependents(candidate, worktree)
        finally:
            os.chmod(handoff_dir, original_mode)

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate", (
            f"non-empty scan_errors must map to state=='indeterminate', NEVER "
            f"'none' — got {result!r}"
        )
        assert result["state"] != "none", (
            "indeterminate and none must be distinguishable — this is the "
            "exact conflation this guard exists to prevent"
        )
        assert result["scan_errors"], (
            f"expected non-empty scan_errors on the indeterminate path; got {result!r}"
        )
        assert result["error"] is not None

    def test_no_resolvable_identifier_yields_indeterminate(self, worktree: Path) -> None:
        """Item 4: a candidate with no resolvable identifier (no stub_id, no
        id, no handoff_id) -> state=="indeterminate"."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "no-id-candidate.md",
            {"status": "open"},
        )

        result = blocked_by_dependents(candidate, worktree)

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate", (
            f"a candidate with no resolvable identifier must fail closed to "
            f"'indeterminate'; got {result!r}"
        )
        assert result["error"], (
            f"expected a non-None error naming the missing identifier; got {result!r}"
        )

    def test_exclude_drops_named_path_from_scan_set(self, worktree: Path) -> None:
        """Item 5: `exclude` drops the named path from the scan set
        (resolved-absolute comparison) — a would-be dependent that is
        excluded no longer counts."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "candidate5.md",
            {"stub_id": "cand-05", "status": "open"},
        )
        referrer = _write_handoff_fm(
            handoff_dir / "referrer5.md",
            {"stub_id": "ref-05", "status": "open", "blocked_by": ["cand-05"]},
        )

        result = blocked_by_dependents(
            candidate, worktree, exclude=[str(referrer.resolve())]
        )

        _assert_five_key_shape(result)
        assert result["state"] == "none", (
            f"the sole referrer was excluded from the scan set; expected "
            f"state=='none', got {result!r}"
        )
        assert str(referrer.resolve()) not in result["dependents"]

    def test_malformed_blocked_by_shape_fails_closed_to_indeterminate(
        self, worktree: Path
    ) -> None:
        """Review: code-reviewer (P2, Finding 5) — a LIVE handoff whose
        `blocked_by` field is present but not a str/list/tuple (e.g. a dict,
        from malformed YAML) must fail CLOSED to state=="indeterminate", not
        be silently treated as "does not reference the candidate". Conflating
        "we could not fully look" with "we looked and found nothing" is the
        exact failure this resolver's tri-state contract exists to prevent."""
        from coordinator_core.ops.handoff_children import blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "candidate-malformed.md",
            {"stub_id": "cand-malformed", "status": "open"},
        )
        malformed_path = handoff_dir / "malformed-referrer.md"
        malformed_path.parent.mkdir(parents=True, exist_ok=True)
        malformed_path.write_text(
            "---\n"
            "stub_id: ref-malformed\n"
            "status: open\n"
            "blocked_by:\n"
            "  a: 1\n"
            "---\n\n"
            "Body.\n",
            encoding="utf-8",
        )

        result = blocked_by_dependents(candidate, worktree)

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate", (
            f"a malformed blocked_by shape on a live handoff must fail closed "
            f"to 'indeterminate', never 'none'; got {result!r}"
        )
        assert result["scan_errors"], (
            f"expected a non-empty scan_errors entry naming the malformed "
            f"handoff; got {result!r}"
        )
        assert result["error"] is not None

class TestBlockedByDependentsOp:
    """`handoff.blocked_by_dependents` — registered op wrapper around
    `blocked_by_dependents` (PIN-1 registration, cross-repo/inbox/2026-08-02-
    doe-claude-em-baton-lifecycle-three-asks-reply.md Ask 3). Mirrors the
    op-level test shape used for `handoff.has_live_children` above: router
    supplies repo_root=<worktree>/.git (the common_dir), the op maps it via
    main_worktree_root before delegating to `blocked_by_dependents`."""

    def test_dependents_present_survives_op_boundary(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "op-candidate.md",
            {"stub_id": "op-cand-01", "status": "open"},
        )
        referrer = _write_handoff_fm(
            handoff_dir / "op-referrer.md",
            {"stub_id": "op-ref-01", "status": "open", "blocked_by": ["op-cand-01"]},
        )

        common_dir = worktree / ".git"
        result = _handoff_blocked_by_dependents(
            params={"candidate": str(candidate)},
            repo_root=common_dir,
        )

        _assert_five_key_shape(result)
        assert result["state"] == "dependents", (
            f"expected state=='dependents' across the op boundary; got {result!r}"
        )
        assert str(referrer.resolve()) in result["dependents"]
        assert result["error"] is None

    def test_no_dependents_yields_none(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        handoff_dir = worktree / "state" / "handoffs"
        candidate = _write_handoff_fm(
            handoff_dir / "op-candidate2.md",
            {"stub_id": "op-cand-02", "status": "open"},
        )

        common_dir = worktree / ".git"
        result = _handoff_blocked_by_dependents(
            params={"candidate": str(candidate)},
            repo_root=common_dir,
        )

        _assert_five_key_shape(result)
        assert result["state"] == "none", f"expected state=='none'; got {result!r}"
        assert result["dependents"] == []
        assert result["error"] is None

    @_SKIP_CHMOD
    def test_scan_error_surfaces_as_indeterminate_not_none_across_op_boundary(
        self, worktree: Path
    ) -> None:
        """The tri-state case that matters: a scan error underneath the
        op boundary must surface as state=="indeterminate", NEVER a quiet
        "none" — this is the exact protection DoE's accepted reply named
        as the reason to keep `indeterminate` loud through registration."""
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        archive_dir = worktree / "archive" / "handoffs"
        candidate = _write_handoff_fm(
            archive_dir / "op-candidate3.md",
            {"stub_id": "op-cand-03", "status": "consumed"},
        )

        handoff_dir = worktree / "state" / "handoffs"
        original_mode = handoff_dir.stat().st_mode
        os.chmod(handoff_dir, 0o000)
        try:
            common_dir = worktree / ".git"
            result = _handoff_blocked_by_dependents(
                params={"candidate": str(candidate)},
                repo_root=common_dir,
            )
        finally:
            os.chmod(handoff_dir, original_mode)

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate", (
            f"a scan error crossing the op boundary must stay 'indeterminate', "
            f"never degrade to a quiet 'none'; got {result!r}"
        )
        assert result["scan_errors"]
        assert result["error"] is not None

    def test_missing_candidate_param_yields_indeterminate(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        common_dir = worktree / ".git"
        result = _handoff_blocked_by_dependents(params={}, repo_root=common_dir)

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate"
        assert result["error"]

    def test_no_repo_root_yields_indeterminate(self) -> None:
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        result = _handoff_blocked_by_dependents(
            params={"candidate": "/does/not/matter"}, repo_root=None
        )

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate"
        assert result["error"]

    def test_candidate_escaping_allowed_roots_yields_indeterminate(
        self, worktree: Path, tmp_path: Path
    ) -> None:
        from coordinator_core.ops.handoff_children import _handoff_blocked_by_dependents

        outside = tmp_path / "outside" / "not-a-handoff.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("---\nstub_id: escapee\n---\n\nBody.\n", encoding="utf-8")

        common_dir = worktree / ".git"
        result = _handoff_blocked_by_dependents(
            params={"candidate": str(outside)}, repo_root=common_dir
        )

        _assert_five_key_shape(result)
        assert result["state"] == "indeterminate"
        assert "escapes" in (result["error"] or "")


class TestBlockedByDependentsPinnedFunctionUnchanged:
    def test_default_edge_kinds_unwidened(self) -> None:
        """Item 7 (PIN test, AC3): `_handoff_has_live_children`'s
        `_DEFAULT_EDGE_KINDS` is unchanged by this plan. This assertion
        legitimately PASSES against current HEAD (a pin, not a regression) —
        `blocked_by_dependents` is a NEW resolver alongside this constant, not
        a replacement for it, and AC3 requires the existing edge-kind set stay
        exactly {"predecessor", "additional_predecessors", "forked_from"}."""
        from coordinator_core.ops.handoff_children import _DEFAULT_EDGE_KINDS

        assert _DEFAULT_EDGE_KINDS == {
            "predecessor",
            "additional_predecessors",
            "forked_from",
        }, (
            f"_DEFAULT_EDGE_KINDS must stay exactly the three lineage edge "
            f"kinds — blocked_by is a separate resolver, not a widening of "
            f"this set; got {_DEFAULT_EDGE_KINDS!r}"
        )
