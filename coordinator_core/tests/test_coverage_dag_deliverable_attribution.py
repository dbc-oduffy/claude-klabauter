"""
coordinator_core.tests.test_coverage_dag_deliverable_attribution — regression
tests for DAG-mode chain_set segment-attribution over-attribution via
Session-Id (2026-07-27 fix).

Root cause (coordinator_core/coverage.py, _derive_dag_chain_set Step 3, prior
to this fix): once a coverable node's authoring Session-Id is resolved, its
credited commit segment was the FULL set of commits carrying that
Session-Id trailer — i.e. every commit the authoring session ever made, not
just the commits belonging to the node's own workstream. A session that
spins off a baton and also ships unrelated work donates all of it to the
spinoff's review obligation. Reported independently by example-retrieval-repo and
Example-cockpit-repo (2026-07-27); a worked instance attributed 8 commits to a
chain with zero file overlap with the workstream.

Fix: when a node's handoff carries a `deliverable_id` frontmatter field, its
segment is the UNION of (a) commits whose `Deliverable-Id` trailer equals
that id, and (b) commits matching the node's Session-Id that carry NO
Deliverable-Id trailer at all (the legacy-history fallback — this leg is
what keeps existing untrailered commits attributed exactly as before). Nodes
without a deliverable_id keep the plain Session-Id segment, unchanged.

Follows the fixture-building pattern established in
test_coverage_dag_chain_set_cross_branch.py (_init_repo / _git).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
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
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


_SESSION_ID = "55555555-5555-5555-5555-555555555555"
_DELIVERABLE_ID = "dlv-worked-instance-abc123"
_OTHER_DELIVERABLE_ID = "dlv-unrelated-workstream-def456"


def _make_closing_handoff(repo: Path, deliverable_id: str) -> Path:
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    closing = handoffs / "closing.md"
    closing.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: none\n"
        f"deliverable_id: {deliverable_id}\n"
        "---\n"
        "Closing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {_SESSION_ID}"],
        repo,
    )
    return closing


def test_commit_with_different_deliverable_id_excluded(tmp_path: Path) -> None:
    """The reported defect: a commit stamped with a DIFFERENT Deliverable-Id
    HELD BY ANOTHER WALKED NODE, but the SAME Session-Id, must be excluded
    from this node's own segment.

    2026-07-31 AC0 refinement: the exclusion in `_derive_dag_chain_set`'s
    legacy leg is scoped to deliverable_ids held by WALKED nodes (a set
    collected before the per-node loop), not truthiness on ANY
    Deliverable-Id trailer — a commit stamped with an UNWALKED node's
    deliverable_id must remain a member of chain_set (see
    coordinator/tests/test_review_coverage_gate_derivation.py's
    AC0-named tests). This test now builds a second, WALKED ancestor node
    that legitimately owns `_OTHER_DELIVERABLE_ID`, so the exclusion this
    test pins is the one AC0 actually intends: excluded from THIS node's
    segment, not vanished from chain_set outright (the foreign commit is
    still credited, correctly, to the ancestor node that owns the
    deliverable it is stamped with).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # A second WALKED node (predecessor of `closing`) that legitimately owns
    # `_OTHER_DELIVERABLE_ID` — without this, `_OTHER_DELIVERABLE_ID` would be
    # unwalked and the foreign commit below must remain in `closing`'s own
    # segment per AC0, not be excluded.
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    ancestor = handoffs / "ancestor.md"
    ancestor.write_text(
        "---\n"
        "session_id: s0\n"
        "predecessor: none\n"
        f"deliverable_id: {_OTHER_DELIVERABLE_ID}\n"
        "---\n"
        "Ancestor body.\n"
    )
    _git(["add", "state/handoffs/ancestor.md"], repo)
    ancestor_session_id = "77777777-7777-7777-7777-777777777777"
    _git(
        ["commit", "-m", f"add ancestor handoff\n\nSession-Id: {ancestor_session_id}"],
        repo,
    )

    handoffs_closing = handoffs / "closing.md"
    handoffs_closing.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: ancestor.md\n"
        f"deliverable_id: {_DELIVERABLE_ID}\n"
        "---\n"
        "Closing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {_SESSION_ID}"],
        repo,
    )
    closing = handoffs_closing
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # Same session as `closing`, but a deliverable HELD BY THE WALKED
    # ancestor node — must be excluded from `closing`'s own segment.
    foreign = repo / "foreign.txt"
    foreign.write_text("unrelated deliverable work\n")
    _git(["add", "foreign.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"unrelated work\n\nSession-Id: {_SESSION_ID}\n"
            f"Deliverable-Id: {_OTHER_DELIVERABLE_ID}",
        ],
        repo,
    )
    foreign_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert add_sha in result.shas
    closing_attribution = result.node_attribution[str(closing.resolve())]
    assert foreign_sha not in closing_attribution.shas, (
        "a commit stamped with a deliverable held by another WALKED node "
        "must be excluded from this node's own segment even though it "
        f"shares this node's Session-Id; shas={closing_attribution.shas!r}"
    )
    # Still credited overall — correctly, to the ancestor node that owns
    # `_OTHER_DELIVERABLE_ID` via leg (a) — not vanished from chain_set.
    assert foreign_sha in result.shas, (
        "excluded-from-this-node's-segment must not mean dropped from "
        f"chain_set entirely; shas={result.shas!r}"
    )


def test_untrailered_commit_same_session_id_still_included(tmp_path: Path) -> None:
    """Leg (b), the legacy-history fallback: an untrailered commit (no
    Deliverable-Id at all) matching the node's Session-Id is still included —
    this is what makes the change no-regression over pre-trailer history.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    closing = _make_closing_handoff(repo, _DELIVERABLE_ID)
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    legacy = repo / "legacy.txt"
    legacy.write_text("legacy untrailered work\n")
    _git(["add", "legacy.txt"], repo)
    _git(
        ["commit", "-m", f"legacy work\n\nSession-Id: {_SESSION_ID}"],
        repo,
    )
    legacy_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert add_sha in result.shas
    assert legacy_sha in result.shas, (
        "an untrailered commit sharing this node's Session-Id must still be "
        f"included (legacy leg (b)); shas={result.shas!r}"
    )


def test_matching_deliverable_id_different_session_id_included(tmp_path: Path) -> None:
    """Leg (a): a commit stamped with the MATCHING Deliverable-Id but a
    DIFFERENT Session-Id is included — attribution follows the work, not
    the typist.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    closing = _make_closing_handoff(repo, _DELIVERABLE_ID)
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    other_session_id = "66666666-6666-6666-6666-666666666666"
    same_work = repo / "same_work.txt"
    same_work.write_text("same deliverable, different typist\n")
    _git(["add", "same_work.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"same deliverable work\n\nSession-Id: {other_session_id}\n"
            f"Deliverable-Id: {_DELIVERABLE_ID}",
        ],
        repo,
    )
    same_work_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert add_sha in result.shas
    assert same_work_sha in result.shas, (
        "a commit stamped with the matching Deliverable-Id must be included "
        f"even under a different Session-Id; shas={result.shas!r}"
    )


def test_no_deliverable_id_behaviour_unchanged(tmp_path: Path) -> None:
    """A node with no deliverable_id keeps today's plain Session-Id
    attribution — byte-identical to pre-existing behaviour.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    closing = handoffs / "closing.md"
    closing.write_text("---\nsession_id: s1\npredecessor: none\n---\nClosing body.\n")
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {_SESSION_ID}"],
        repo,
    )
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # Same session, carries a Deliverable-Id trailer -- irrelevant here since
    # this node has no deliverable_id of its own; legacy behaviour credits
    # every commit sharing the Session-Id regardless of Deliverable-Id.
    other = repo / "other.txt"
    other.write_text("other work, same session\n")
    _git(["add", "other.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"other work\n\nSession-Id: {_SESSION_ID}\n"
            f"Deliverable-Id: {_OTHER_DELIVERABLE_ID}",
        ],
        repo,
    )
    other_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert add_sha in result.shas
    assert other_sha in result.shas, (
        "no deliverable_id on this node -> legacy behaviour must be "
        f"unchanged (all Session-Id commits included); shas={result.shas!r}"
    )


def test_malformed_deliverable_id_is_indeterminate(tmp_path: Path) -> None:
    """Fidelity guard 1 (deliverable variant): a deliverable_id containing
    regex metacharacters must fail closed to INDETERMINATE, never COVERED.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    malformed_id = "dlv-.*evil"
    closing = _make_closing_handoff(repo, malformed_id)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        "a malformed deliverable_id must fail closed to INDETERMINATE, "
        f"got shas={result.shas!r} notes={result.notes!r}"
    )
    assert any("deliverable_id" in n for n in result.notes)
