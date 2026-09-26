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


def _run(coro):
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


class _FakeCtx:
    repo_root = None


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


class TestLiveChildDetected:

    def test_exit_code_0_when_child_references_candidate(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        handoff_dir = worktree / "state" / "handoffs"

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
        from coordinator_core.ops.handoff_children import _collect_handoff_paths

        paths, scan_errors = _collect_handoff_paths(worktree / ".git")
        assert paths == [], (
            f"expected [] when _collect_handoff_paths given raw git common dir; got: {paths}"
        )
        assert scan_errors == [], (
            "neither subtree exists under <worktree>/.git — an absent subtree is NOT a "
            f"scan error (only an unreadable-but-present one is); got: {scan_errors}"
        )


class TestNoLiveChildren:

    def test_exit_code_1_when_no_child_references_candidate(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        handoff_dir = worktree / "state" / "handoffs"

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


class TestArchiveSubtree:

    def test_candidate_in_archive_child_in_state(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        archive_dir = worktree / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        state_dir = worktree / "state" / "handoffs"

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
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

        archive_dir = worktree / "archive" / "handoffs" / "2026-06"
        archive_dir.mkdir(parents=True)
        state_dir = worktree / "state" / "handoffs"

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


class TestIndeterminate:

    def test_indeterminate_when_no_repo_root(self, tmp_path: Path) -> None:
        from coordinator_core.ops.handoff_children import _handoff_has_live_children

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


class TestInitiativesServeFailClosed:

    def test_empty_set_when_no_repo_root(self) -> None:
        from coordinator_core.ops.initiatives_serve import _handler as _initiatives_handler

        result = _initiatives_handler(params={}, repo_root=None)

        assert result == {"initiatives": []}, (
            f"expected {{'initiatives': []}} when repo_root=None; got {result}"
        )


_SKIP_CHMOD = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)


class TestUnscannableSubtreeFailsClosed:

    @_SKIP_CHMOD
    def test_unreadable_state_handoffs_dir_fails_closed(self, worktree: Path) -> None:
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


# RED-BEFORE-GREEN: `blocked_by_dependents` does not exist on disk yet at the
# `_DEFAULT_EDGE_KINDS` pin test below, must keep passing untouched.


def _write_handoff_fm(path: Path, fields: "dict[str, object]", body: str = "Body.\n") -> Path:
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
    for key in ("state", "dependents", "identifiers", "scan_errors", "error"):
        assert key in result, f"blocked_by_dependents result missing key {key!r}: {result!r}"
    assert result["state"] in ("dependents", "none", "indeterminate"), (
        f"state must be one of the tri-state values; got {result['state']!r}"
    )


class TestBlockedByDependents:

    def test_live_referrer_via_blocked_by_yields_dependents(self, worktree: Path) -> None:
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

class TestBlockedByDependentsMany:

    def test_batched_verdicts_match_the_singular_resolver(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import (
            blocked_by_dependents,
            blocked_by_dependents_many,
        )

        handoff_dir = worktree / "state" / "handoffs"
        with_dep = _write_handoff_fm(
            handoff_dir / "many-with-dep.md",
            {"stub_id": "many-01", "status": "open"},
        )
        without_dep = _write_handoff_fm(
            handoff_dir / "many-without-dep.md",
            {"stub_id": "many-02", "status": "open"},
        )
        terminal_only = _write_handoff_fm(
            handoff_dir / "many-terminal-only.md",
            {"stub_id": "many-03", "status": "open"},
        )
        _write_handoff_fm(
            handoff_dir / "many-referrer.md",
            {"stub_id": "many-ref", "status": "open", "blocked_by": ["many-01"]},
        )
        _write_handoff_fm(
            handoff_dir / "many-terminal-referrer.md",
            {"stub_id": "many-term", "status": "superseded", "blocked_by": ["many-03"]},
        )
        no_identifier = _write_handoff_fm(
            handoff_dir / "many-no-id.md",
            {"status": "open"},
        )

        candidates = [with_dep, without_dep, terminal_only, no_identifier]
        batched = blocked_by_dependents_many(candidates, worktree)

        assert set(batched) == {str(c) for c in candidates}, (
            f"every candidate must get a reply, keyed exactly as passed; got "
            f"{sorted(batched)!r}"
        )
        for candidate in candidates:
            singular = blocked_by_dependents(candidate, worktree)
            _assert_five_key_shape(batched[str(candidate)])
            assert batched[str(candidate)] == singular, (
                f"batched verdict for {candidate} diverges from the singular "
                f"resolver: {batched[str(candidate)]!r} != {singular!r}"
            )

        assert batched[str(with_dep)]["state"] == "dependents"
        assert batched[str(without_dep)]["state"] == "none"
        assert batched[str(terminal_only)]["state"] == "none"
        assert batched[str(no_identifier)]["state"] == "indeterminate"

    def test_walks_the_corpus_once_for_many_candidates(
        self, worktree: Path, monkeypatch
    ) -> None:
        from coordinator_core.ops import handoff_children
        from coordinator_core.reconcile import handoff_corpus

        handoff_dir = worktree / "state" / "handoffs"
        candidates = [
            _write_handoff_fm(
                handoff_dir / f"walk-candidate-{n}.md",
                {"stub_id": f"walk-{n}", "status": "open"},
            )
            for n in range(4)
        ]
        _write_handoff_fm(
            handoff_dir / "walk-referrer.md",
            {"stub_id": "walk-ref", "status": "open", "blocked_by": ["walk-0", "walk-2"]},
        )

        real_collect = handoff_corpus._collect_all_handoffs_for_gate_index
        calls = 0

        def _counting_collect(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_collect(*args, **kwargs)

        monkeypatch.setattr(
            handoff_corpus, "_collect_all_handoffs_for_gate_index", _counting_collect
        )

        result = handoff_children.blocked_by_dependents_many(candidates, worktree)

        assert calls == 1, (
            f"{len(candidates)} candidates must cost exactly one corpus walk; "
            f"got {calls}"
        )
        assert result[str(candidates[0])]["state"] == "dependents"
        assert result[str(candidates[1])]["state"] == "none"
        assert result[str(candidates[2])]["state"] == "dependents"
        assert result[str(candidates[3])]["state"] == "none"

    def test_no_identifier_candidate_does_not_contaminate_its_siblings(
        self, worktree: Path
    ) -> None:
        from coordinator_core.ops.handoff_children import blocked_by_dependents_many

        handoff_dir = worktree / "state" / "handoffs"
        no_identifier = _write_handoff_fm(
            handoff_dir / "sibling-no-id.md",
            {"status": "open"},
        )
        resolvable = _write_handoff_fm(
            handoff_dir / "sibling-resolvable.md",
            {"stub_id": "sibling-01", "status": "open"},
        )
        referrer = _write_handoff_fm(
            handoff_dir / "sibling-referrer.md",
            {"stub_id": "sibling-ref", "status": "open", "blocked_by": ["sibling-01"]},
        )

        result = blocked_by_dependents_many([no_identifier, resolvable], worktree)

        assert result[str(no_identifier)]["state"] == "indeterminate"
        assert result[str(resolvable)]["state"] == "dependents", (
            f"an unresolvable sibling must not fail the whole batch closed; "
            f"got {result[str(resolvable)]!r}"
        )
        assert str(referrer.resolve()) in result[str(resolvable)]["dependents"]

    def test_malformed_blocked_by_on_the_candidate_itself_is_still_skipped(
        self, worktree: Path
    ) -> None:
        from coordinator_core.ops.handoff_children import (
            blocked_by_dependents,
            blocked_by_dependents_many,
        )

        handoff_dir = worktree / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        self_malformed = handoff_dir / "self-malformed.md"
        self_malformed.write_text(
            "---\n"
            "stub_id: self-malformed\n"
            "status: open\n"
            "blocked_by:\n"
            "  a: 1\n"
            "---\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        _write_handoff_fm(
            handoff_dir / "self-malformed-referrer.md",
            {
                "stub_id": "self-malformed-ref",
                "status": "open",
                "blocked_by": ["self-malformed"],
            },
        )

        batched = blocked_by_dependents_many([self_malformed], worktree)[str(self_malformed)]

        assert batched == blocked_by_dependents(self_malformed, worktree)
        assert batched["state"] == "dependents", (
            f"the candidate's OWN malformed blocked_by is skipped before it is "
            f"read; expected state=='dependents', got {batched!r}"
        )

    def test_empty_candidate_list_does_no_work(self, worktree: Path) -> None:
        from coordinator_core.ops.handoff_children import blocked_by_dependents_many

        assert blocked_by_dependents_many([], worktree) == {}


class TestBlockedByDependentsOp:

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
