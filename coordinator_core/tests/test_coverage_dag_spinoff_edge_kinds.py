"""
coordinator_core.tests.test_coverage_dag_spinoff_edge_kinds — regression test for
the Step-1/Step-2 edge-kind asymmetry in coverage._derive_dag_chain_set (§4 of
state/subagent-share/cd088bf4-7907-4294-ac34-8483a43fb981/coordinatorstaff-eng-
0aebc70d.md).

Root cause: Step 1's `dag.walk_forward` call deliberately excludes `forked_from`
(review obligation follows continuation edges — predecessor / additional_predecessors
— not fork ancestry: a spinoff is a niece, not a descendant, and schema rule C2-4
forces a spinoff's `predecessor: none`, so it can never walk back to the node it
forked from). Step 2's `archival.reverse_membership` call took the ARCHIVAL
default instead (all three edge kinds, including `forked_from`) — correct for
"is it safe to archive this node?", wrong for the CONCLUSION-shaped question
Step 2 is actually asking.

Consequence: a live spinoff `F` (`forked_from: A`) makes `reverse_membership`
report `F` as a live child of ancestor `A`, which blocks `A` from ever entering
`closing_set`. Because `chain_set`/`chain_commits` is built ONLY from
`closing_set` (Step 3 iterates `for node in closing_set:`), `A`'s commits never
enter `chain_commits` at all — not counted, not `uncovered`, no waiver, no
note. The verdict tilts toward COVERED (excluding a node from `closing_set` is
the fail-open direction — see coverage.py's module docstring). And because
Step 1 never walks `forked_from` and the spinoff's own `predecessor` is
schema-forced to `none`, `F`'s own later close can never walk back to `A`
either: `A`'s commits become unreviewable by construction.

Fix: `_CONTINUATION_EDGE_KINDS = frozenset({"predecessor",
"additional_predecessors"})` is now the single set BOTH Step 1's `walk_forward`
call and Step 2's `reverse_membership` call use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """dag._FRONTMATTER_CACHE is module-level; clear it so a stale parse from a
    prior test's tmp_path never masks a fresh fixture's frontmatter.
    """
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


_ANCESTOR_SID = "11111111-1111-1111-1111-111111111111"
_CLOSING_SID = "22222222-2222-2222-2222-222222222222"
_THIRD_NODE_SID = "33333333-3333-3333-3333-333333333333"
_LIVE_SPINOFF_SESSION = "spinoff-live-session"


def _build_ancestor_and_closing(repo: Path) -> tuple[Path, Path, str]:
    """ancestor.md (predecessor: none) <- closing.md (predecessor: ancestor.md).
    Returns (ancestor, closing, ancestor_add_sha)."""
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    ancestor = handoffs / "ancestor.md"
    ancestor.write_text("---\nsession_id: s_a\npredecessor: none\n---\nAncestor body.\n")
    _git(["add", "state/handoffs/ancestor.md"], repo)
    _git(
        ["commit", "-m", f"add ancestor handoff\n\nSession-Id: {_ANCESTOR_SID}"],
        repo,
    )
    ancestor_add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s_b\npredecessor: ancestor.md\n---\nClosing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {_CLOSING_SID}"],
        repo,
    )

    return ancestor, closing, ancestor_add_sha


def test_spinoff_does_not_block_ancestor(tmp_path: Path, monkeypatch) -> None:
    """§4 regression: a live spinoff (`forked_from: ancestor.md`, schema-forced
    `predecessor: none`) must NOT block `ancestor.md` from entering closing_set.
    Red before the fix (asserted explicitly below via a temporary revert), green
    after.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    ancestor, closing, ancestor_add_sha = _build_ancestor_and_closing(repo)

    handoffs = repo / "state" / "handoffs"
    spinoff = handoffs / "spinoff.md"
    spinoff.write_text(
        "---\n"
        "session_id: s_f\n"
        "forked_from: ancestor.md\n"
        "predecessor: none\n"
        f"claimed_by: {_LIVE_SPINOFF_SESSION}\n"
        "---\n"
        "Spinoff body.\n"
    )
    _git(["add", "state/handoffs/spinoff.md"], repo)
    _git(
        ["commit", "-m", f"add spinoff handoff\n\nSession-Id: {_THIRD_NODE_SID}"],
        repo,
    )

    monkeypatch.setattr(
        cov, "resolve_live_session_ids", lambda: frozenset({_LIVE_SPINOFF_SESSION})
    )

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert ancestor_add_sha in result.shas, (
        "a live spinoff's forked_from edge must not block its origin ancestor "
        f"from entering closing_set/chain_commits; shas={result.shas!r}, "
        f"notes={result.notes!r}"
    )


def test_archival_default_edge_set_does_block_ancestor(
    tmp_path: Path, monkeypatch
) -> None:
    """Pins the PRE-FIX defect, so the regression test above cannot go vacuous.

    Widens Step 2's edge_kinds back to the archival default (all three kinds,
    incl. `forked_from`) via monkeypatch — the exact shape the source carried
    before this change — and asserts the ancestor IS blocked and its commits DO
    drop out. This test is GREEN: it asserts the old, wrong behaviour under the
    old edge set. If a future refactor makes
    `test_spinoff_does_not_block_ancestor` pass for some reason unrelated to the
    edge set, this one goes red and says so.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    ancestor, closing, ancestor_add_sha = _build_ancestor_and_closing(repo)

    handoffs = repo / "state" / "handoffs"
    spinoff = handoffs / "spinoff.md"
    spinoff.write_text(
        "---\n"
        "session_id: s_f\n"
        "forked_from: ancestor.md\n"
        "predecessor: none\n"
        f"claimed_by: {_LIVE_SPINOFF_SESSION}\n"
        "---\n"
        "Spinoff body.\n"
    )
    _git(["add", "state/handoffs/spinoff.md"], repo)
    _git(
        ["commit", "-m", f"add spinoff handoff\n\nSession-Id: {_THIRD_NODE_SID}"],
        repo,
    )

    monkeypatch.setattr(
        cov, "resolve_live_session_ids", lambda: frozenset({_LIVE_SPINOFF_SESSION})
    )

    # Reproduce the pre-fix shape: Step 2 takes reverse_membership's own
    # ARCHIVAL default (edge_kinds=None -> all three kinds) instead of the
    # narrowed _CONTINUATION_EDGE_KINDS set the fix now passes explicitly.
    real_reverse_membership = cov.reverse_membership

    def _pre_fix_reverse_membership(node_path, dag_index, **kwargs):
        kwargs.pop("edge_kinds", None)
        return real_reverse_membership(node_path, dag_index)

    monkeypatch.setattr(cov, "reverse_membership", _pre_fix_reverse_membership)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert ancestor_add_sha not in result.shas, (
        "sanity check: under the PRE-FIX archival-default edge set, the live "
        "spinoff's forked_from edge DOES block the ancestor -- this pins the "
        "bug the fix closes and proves the fixed-behaviour test above is a "
        f"real regression test, not a vacuous one; shas={result.shas!r}"
    )


def test_live_predecessor_child_still_blocks(tmp_path: Path, monkeypatch) -> None:
    """Over-correction guard: a THIRD node with a live `predecessor: ancestor.md`
    edge (a genuine continuation edge, not a fork) must still block `ancestor.md`
    from entering closing_set — the narrowing must not disable legitimate
    fan-out deferral.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    ancestor, closing, ancestor_add_sha = _build_ancestor_and_closing(repo)

    handoffs = repo / "state" / "handoffs"
    sibling = handoffs / "sibling.md"
    sibling.write_text(
        "---\n"
        "session_id: s_c\n"
        "predecessor: ancestor.md\n"
        f"claimed_by: {_LIVE_SPINOFF_SESSION}\n"
        "---\n"
        "Sibling body.\n"
    )
    _git(["add", "state/handoffs/sibling.md"], repo)
    _git(
        ["commit", "-m", f"add sibling handoff\n\nSession-Id: {_THIRD_NODE_SID}"],
        repo,
    )

    monkeypatch.setattr(
        cov, "resolve_live_session_ids", lambda: frozenset({_LIVE_SPINOFF_SESSION})
    )

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert ancestor_add_sha not in result.shas, (
        "a live sibling's genuine `predecessor` continuation edge must still "
        f"block its ancestor from entering closing_set; shas={result.shas!r}"
    )


def test_derive_dag_chain_set_edge_kinds_symmetric(tmp_path: Path, monkeypatch) -> None:
    """Structural pin: Step 1's walk_forward call and Step 2's reverse_membership
    call must be passed the SAME edge_kinds set. Stops a future reader
    "restoring" the archival default on one leg only.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _, closing, _ = _build_ancestor_and_closing(repo)

    captured: dict = {}

    real_walk_forward = cov.walk_forward

    def _capturing_walk_forward(*args, **kwargs):
        captured["walk_forward_edge_kinds"] = kwargs.get("edge_kinds")
        return real_walk_forward(*args, **kwargs)

    monkeypatch.setattr(cov, "walk_forward", _capturing_walk_forward)

    real_reverse_membership = cov.reverse_membership

    def _capturing_reverse_membership(*args, **kwargs):
        captured.setdefault(
            "reverse_membership_edge_kinds_calls", []
        ).append(kwargs.get("edge_kinds"))
        return real_reverse_membership(*args, **kwargs)

    monkeypatch.setattr(cov, "reverse_membership", _capturing_reverse_membership)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert captured["walk_forward_edge_kinds"] is not None, (
        "walk_forward must be called with an explicit edge_kinds set"
    )
    reverse_calls = captured.get("reverse_membership_edge_kinds_calls", [])
    assert reverse_calls, "reverse_membership must have been called at least once"
    for call_edge_kinds in reverse_calls:
        assert call_edge_kinds == captured["walk_forward_edge_kinds"], (
            "Step 1 (walk_forward) and Step 2 (reverse_membership) must be "
            f"passed the SAME edge_kinds set; walk_forward={captured['walk_forward_edge_kinds']!r}, "
            f"reverse_membership={call_edge_kinds!r}"
        )
