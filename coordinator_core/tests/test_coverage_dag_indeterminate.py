"""
coordinator_core.tests.test_coverage_dag_indeterminate — regression tests locking
the DAG-mode fidelity guards in coverage._derive_dag_chain_set that must
classify INDETERMINATE (exit 2) rather than collapsing to COVERED/UNCOVERED,
plus the graceful-degradation carve-out for untrailered ANCESTOR nodes:

    1. no-trailer-ancestor          — an inherited ANCESTOR's add-commit exists
                                       but carries no Session-Id trailer. As of
                                       the 2026-07-22 over-strictness fix, this
                                       degrades to skip-with-note (that node's
                                       segment is simply not attributed) and the
                                       walk continues — it must NOT poison the
                                       whole chain to INDETERMINATE when the
                                       rest of the chain (in particular the
                                       closing/current session's own node) is
                                       attributable.
    1b. no-trailer-closing-node     — the CLOSING/current session's own node
                                       (the --from-handoff node THIS run is
                                       responsible for) still hard-fails
                                       INDETERMINATE if its own add-commit is
                                       untrailered. This is the preserved
                                       safety guard — only inherited ancestors
                                       degrade, never the responsible node.
    2. already-merged-null-attribution — a coverable ancestor has NO discoverable
                                       add-commit at all (git log --follow
                                       --diff-filter=A finds nothing — e.g. the
                                       file was never committed under that path,
                                       the "attribution gap" case).
    3. vacuous-match                — a valid, UUID-shaped Session-Id matches
                                       ZERO commits under `git log --all
                                       --no-merges --grep=...` (dead/rewritten
                                       history).

Routed from example-doctrine-repo bug-blitz 2026-07-19 (state/bug-backlog/
2026-07-17-coverage-py-indeterminate-paths-regressed.yaml) as a P1: all three
original cases were reported as regressed, collapsing to COVERED/NOT-COVERED
instead of INDETERMINATE. Investigation found _derive_dag_chain_set's guard
code intact and git history shows no commit had touched this function since
the original port (526ec475) — these tests exist because the function had
ZERO prior coverage, not because a fix was needed; they pin the guards against
a future regression regardless of how the original report arose.

Case 1 was revised 2026-07-22 (coordinator_core/coverage.py ~:1029) after a
real ANCESTOR handoff add-commit (53fa32a7, a spinoff-add authored without a
Session-Id trailer) poisoned a chain-end DAG verdict to INDETERMINATE for a
later, fully-reviewed closing session — see the Part A fix in
coordinator_core/coverage.py's segment-attribution loop.

Cases are exercised against REAL git repos/commits (no mocking) where
practical — the strongest possible evidence the guards fire in production.
Case 3 (a valid Session-Id trailer matching zero commits) has no clean
real-git construction (the add-commit that carries a real trailer is, by
construction, also a real non-merge commit that `--grep` would find), so only
its final `git log --all --no-merges --grep=...` call is monkeypatched to
force the zero-match condition; the preceding add-commit + trailer lookups
stay real.
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
    prior test's tmp_path (unlikely but possible on path reuse) never masks a
    fresh fixture's frontmatter. Mirrors test_cache_coherency.py's fixture.
    """
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def test_derive_dag_chain_set_no_trailer_ancestor(tmp_path: Path) -> None:
    """Case 1: an inherited ANCESTOR's add-commit exists but has no Session-Id
    trailer — must degrade to skip-with-note, NOT poison the whole chain.

    Real git repo, real commits, no mocking. Ancestor has no live blockers
    (its only referencing edge is the closing handoff, which starts in
    closing_set), so it becomes coverable in the fixpoint and reaches segment
    attribution — where the missing trailer must be skipped (note appended,
    contributes nothing to `covered`) while the closing node's own segment
    (fully attributable via its own trailered add-commit) still resolves the
    chain to a non-indeterminate verdict.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    ancestor = handoffs / "ancestor.md"
    ancestor.write_text("---\nsession_id: s2\n---\nAncestor body.\n")
    _git(["add", "state/handoffs/ancestor.md"], repo)
    _git(["commit", "-m", "add ancestor handoff\n\nNo trailer here."], repo)

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s1\npredecessor: ancestor.md\n---\nClosing body.\n"
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

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, (
        f"an untrailered ANCESTOR add-commit must degrade to skip-with-note, "
        f"not poison the whole chain to INDETERMINATE, when the rest of the "
        f"chain (the closing node) is attributable; notes={result.notes!r}"
    )
    assert any(
        "ancestor add-commit" in note and "untrailered" in note and "skipped" in note
        for note in result.notes
    ), f"expected an 'ancestor add-commit ... untrailered ... skipped' note; got {result.notes!r}"
    assert result.shas, (
        "the closing node's own trailered segment must still be attributed "
        f"(never falsely empty/COVERED); shas={result.shas!r}"
    )


def test_derive_dag_chain_set_closing_node_no_trailer_still_indeterminate(
    tmp_path: Path,
) -> None:
    """Case 1b (preserved safety guard): the CLOSING/current session's OWN
    node — the --from-handoff node THIS run is responsible for — must still
    hard-fail INDETERMINATE if ITS OWN add-commit is untrailered. Only
    inherited ancestors get the skip-with-note degradation from Case 1; the
    responsible node never does.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    # No predecessor at all — this is a single-node chain: the closing
    # handoff's own add-commit is what must be attributed, and it is untrailered.
    closing = handoffs / "closing.md"
    closing.write_text("---\nsession_id: s1\n---\nClosing body.\n")
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(["commit", "-m", "add closing handoff\n\nNo trailer here."], repo)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        f"the closing/current session's own node must still classify "
        f"INDETERMINATE when its own add-commit is untrailered — this guard "
        f"must NOT be weakened by the ancestor skip-with-note degradation; "
        f"notes={result.notes!r}"
    )
    assert any(
        "no Session-Id trailer" in note for note in result.notes
    ), f"expected a 'no Session-Id trailer' note; got {result.notes!r}"


def test_derive_dag_chain_set_already_merged_null_attribution(tmp_path: Path) -> None:
    """Case 2: ancestor has NO discoverable add-commit at all (attribution gap).

    Real git repo. The ancestor handoff file exists on disk but was never
    committed under that path (git log --follow --diff-filter=A finds
    nothing) — the "already merged" / squashed-history shape where the file's
    creation is not attributable to any commit reachable via --follow.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    # Ancestor exists on disk but is never git-added/committed.
    ancestor = handoffs / "ancestor.md"
    ancestor.write_text("---\nsession_id: s2\n---\nAncestor body.\n")

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s1\npredecessor: ancestor.md\n---\nClosing body.\n"
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

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        f"an ancestor with no discoverable add-commit must classify "
        f"INDETERMINATE, not collapse to COVERED/UNCOVERED; notes={result.notes!r}"
    )
    assert any("no add-commit found" in note for note in result.notes), (
        f"expected a 'no add-commit found (attribution gap)' note; got {result.notes!r}"
    )


def test_derive_dag_chain_set_vacuous_match(tmp_path: Path, monkeypatch) -> None:
    """Case 3: a valid, UUID-shaped Session-Id matches zero commits under the
    segment-enumeration grep.

    Real git repo/commits for the add-commit + trailer lookup (so this pins
    the real UUID-validation and add-commit resolution paths); only the final
    `git log --all --no-merges --grep=...` segment-enumeration call is forced
    to return zero matches — the one leg with no clean real-git construction
    (a commit whose trailer is discoverable via --diff-filter=A is, by
    construction, also a real non-merge commit `--grep` would find).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    ancestor = handoffs / "ancestor.md"
    ancestor.write_text("---\nsession_id: s2\n---\nAncestor body.\n")
    _git(["add", "state/handoffs/ancestor.md"], repo)
    _git(
        [
            "commit", "-m",
            "add ancestor handoff\n\n"
            "Session-Id: 22222222-2222-2222-2222-222222222222",
        ],
        repo,
    )

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s1\npredecessor: ancestor.md\n---\nClosing body.\n"
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

    real_run = cov._run

    def _fake_run(cmd, cwd=None):
        if "--grep=" in " ".join(cmd):
            return (0, "", "")  # force zero matches on the segment-enumeration call
        return real_run(cmd, cwd=cwd)

    monkeypatch.setattr(cov, "_run", _fake_run)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        f"a valid Session-Id matching zero commits must classify INDETERMINATE, "
        f"not collapse to COVERED/UNCOVERED; notes={result.notes!r}"
    )
    assert any("vacuous-match guard" in note for note in result.notes), (
        f"expected a 'vacuous-match guard' note; got {result.notes!r}"
    )
