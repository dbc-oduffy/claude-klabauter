"""
coordinator_core.tests.test_coverage_dag_stale_liveness_sweep — regression test for
the stale-`live_sids`-snapshot bug in coverage._derive_dag_chain_set's fixpoint.

Prior behaviour: `live_sids = resolve_live_session_ids()` was hoisted ONCE before the
fixpoint's `while changed:` loop, to avoid repeated shell-outs per ancestor. If a
blocker's owning session died partway through the fixpoint's OWN execution (crash, or
a concurrent session legitimately closing that handoff mid-walk), later sweeps still
checked liveness against the stale snapshot — guard 3 (all-stale-blockers) never
fired for that ancestor, wrongly keeping it NOT coverable: a false-UNCOVERED verdict.

Fix: `live_sids` is now re-resolved once per fixpoint SWEEP (once per full pass over
`ancestors`, not once globally and not once per ancestor) — bounds the shell-out cost
to O(sweeps) while observing a mid-walk session death on the very next sweep.

This test constructs a two-ancestor chain where sweep 1 makes progress on one
ancestor (proving there IS a sweep 2) while a second ancestor stays blocked by an
external node whose session is "live" per sweep 1's snapshot and "dead" per sweep 2's
— i.e. `resolve_live_session_ids()` is monkeypatched to return a different value on
its second call. Only a per-sweep re-resolution can observe that transition and
correctly mark the second ancestor coverable.

Routed from cross-repo/inbox/2026-07-24-example-doctrine-repo-em-chain-derivation-reliability-
defects.md §2 (DAG false-COVERED/false-UNCOVERED on shared branches) — this repro
targets the false-UNCOVERED direction specifically.
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


def _build_chain(repo: Path) -> tuple[Path, Path, Path, str]:
    """closing -> ancestor2 -> ancestor1 (predecessor edges). Returns
    (closing, ancestor1, ancestor2, ancestor2_add_sha)."""
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    ancestor1 = handoffs / "ancestor1.md"
    ancestor1.write_text("---\nsession_id: s2\n---\nAncestor1 body.\n")
    _git(["add", "state/handoffs/ancestor1.md"], repo)
    _git(
        [
            "commit", "-m",
            "add ancestor1 handoff\n\n"
            "Session-Id: 11111111-1111-1111-1111-111111111111",
        ],
        repo,
    )

    ancestor2 = handoffs / "ancestor2.md"
    ancestor2.write_text(
        "---\nsession_id: s3\npredecessor: ancestor1.md\n---\nAncestor2 body.\n"
    )
    _git(["add", "state/handoffs/ancestor2.md"], repo)
    _git(
        [
            "commit", "-m",
            "add ancestor2 handoff\n\n"
            "Session-Id: 22222222-2222-2222-2222-222222222222",
        ],
        repo,
    )
    ancestor2_add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s1\npredecessor: ancestor2.md\n---\nClosing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        [
            "commit", "-m",
            "add closing handoff\n\n"
            "Session-Id: 33333333-3333-3333-3333-333333333333",
        ],
        repo,
    )

    return closing, ancestor1, ancestor2, ancestor2_add_sha


def _apply_common_monkeypatches(monkeypatch, calls: dict) -> None:
    """Ancestor1 has no blockers (coverable in sweep 1, driving a sweep 2).
    Ancestor2 is blocked only by an external 'blocker.md' node whose session
    resolve_live_session_ids() reports live on the FIRST call and dead on every
    call after — simulating the blocker's session dying mid-fixpoint.
    """

    def _fake_reverse_membership(node, dag_index, **kwargs):
        if node.endswith("ancestor2.md"):
            return frozenset({"blocker.md"})
        return frozenset()

    monkeypatch.setattr(cov, "reverse_membership", _fake_reverse_membership)

    def _fake_parse_consumed_by(path):
        assert path == "blocker.md", f"unexpected consumed_by lookup for {path!r}"
        return "blocker-sid"

    monkeypatch.setattr(cov, "_parse_handoff_consumed_by", _fake_parse_consumed_by)

    def _fake_live_sids():
        calls["n"] += 1
        if calls["n"] == 1:
            return frozenset({"blocker-sid"})  # sweep 1: blocker's session still live
        return frozenset()  # sweep 2+: blocker's session has died

    monkeypatch.setattr(cov, "resolve_live_session_ids", _fake_live_sids)


def test_per_sweep_refresh_observes_mid_fixpoint_session_death(
    tmp_path: Path, monkeypatch
) -> None:
    """With the per-sweep re-resolution fix, ancestor2 is correctly marked
    coverable once the blocker's session is observed dead on sweep 2 — no
    false-UNCOVERED, and live_sids was genuinely re-resolved more than once.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    closing, ancestor1, ancestor2, ancestor2_add_sha = _build_chain(repo)

    calls = {"n": 0}
    _apply_common_monkeypatches(monkeypatch, calls)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert calls["n"] >= 2, (
        f"expected resolve_live_session_ids() to be re-resolved across multiple "
        f"sweeps (proving per-sweep refresh, not a single global hoist); "
        f"got {calls['n']} call(s)"
    )
    assert result.indeterminate is False, (
        f"a blocker session observed dead on a later sweep must resolve the "
        f"fixpoint, not leave it INDETERMINATE; notes={result.notes!r}"
    )
    assert ancestor2_add_sha in result.shas, (
        f"ancestor2 must be marked coverable once its blocker's session is "
        f"observed dead on sweep 2 (its add-commit {ancestor2_add_sha} must be "
        f"attributed); shas={result.shas!r}, notes={result.notes!r}"
    )


def test_single_global_snapshot_reproduces_false_uncovered(
    tmp_path: Path, monkeypatch
) -> None:
    """Sanity-check the OLD (pre-fix) behaviour directly: a single global
    live_sids snapshot captured once before the loop never observes the
    blocker's mid-fixpoint session death, so ancestor2 is wrongly left
    NOT coverable (false-UNCOVERED) even though sweep 2 genuinely runs.

    This does not re-run production code under the old shape (the fix is
    correct and stays in place) — it exercises the same fixture/monkeypatch
    scaffolding as the test above but calls resolve_live_session_ids() exactly
    once up front, mirroring the removed hoist-once call, to demonstrate the
    single-snapshot verdict this fix replaces.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    closing, ancestor1, ancestor2, ancestor2_add_sha = _build_chain(repo)

    calls = {"n": 0}
    _apply_common_monkeypatches(monkeypatch, calls)

    # Old shape: resolve once, up front, and pin the fixpoint's view of
    # live_sids to that single result for the rest of the run.
    frozen_live_sids = cov.resolve_live_session_ids()
    monkeypatch.setattr(cov, "resolve_live_session_ids", lambda: frozen_live_sids)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False
    assert ancestor2_add_sha not in result.shas, (
        f"under a single global live_sids snapshot, ancestor2's blocker never "
        f"reads as dead, so ancestor2 must be wrongly left uncovered — this "
        f"pins the bug the per-sweep fix closes; shas={result.shas!r}"
    )
