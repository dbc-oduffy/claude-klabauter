"""
coordinator_core.ops.tests.test_coverage_dag_pickup_attribution — the test the
chain-ancestry-waiver mint suite has never had: a handoff AUTHORED by one
session, with the coverage gate RUN under a different session (a "pickup").

Every existing test of `_derive_dag_chain_set` in this tree stubs the
derivation (hand-written `uncovered_shas` on a `CoverageResult`, or a
monkeypatch of `_derive_dag_chain_set` itself) or -- where it does build a
real repo (test_coverage_dag_chain_set_cross_branch.py,
test_coverage_dag_deliverable_attribution.py,
test_coverage_dag_archived_repo_root.py) -- always closes under the SAME
session that authored the handoff. None of them build the pickup shape:
handoff H added by session A's commit, gate run with closing_session_id=B.

Root cause under test (coordinator_core/coverage.py,
_derive_dag_chain_set's Step-3 per-node loop): for the `from_handoff` node
itself, the loop takes a shortcut --

    if node == closing_abs and closing_session_id:
        sid = closing_session_id

-- substituting the RUNNING session's id in place of deriving the author from
the add-commit's `Session-Id` git trailer (the `else` branch every other node
uses). When the runner picks up a handoff someone else authored, this
resolves to the WRONG session id, and the author's own commits (stamped with
their real Session-Id trailer) are never enumerated into that node's segment.

This file owes exactly two assertions, per plan
docs/plans/2026-08-10-chain-attribution-pickup-author.md § C3:

  1. test_pickup_author_commits_not_attributed_TO_BE_FLIPPED_BY_C2 -- the
     behaviour case. Pins TODAY's (wrong) behaviour: author A's commits are
     NOT attributed when runner B closes the handoff. C2 is expected to flip
     this test so it asserts the opposite (A's commits ARE attributed).
  2. test_same_session_author_runner_control_MUST_NOT_CHANGE -- the control.
     Author == runner (the ordinary path). Must stay byte-identical forever;
     this is the property that makes C1/C2 safe by construction. Never mark
     this one as flippable.

Follows the fixture-building pattern established in
test_coverage_dag_chain_set_cross_branch.py (_init_repo / _git helpers) and
test_coverage_dag_deliverable_attribution.py (closing-handoff shape).

Spec backlink: docs/plans/2026-08-10-chain-attribution-pickup-author.md § C3
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


_SESSION_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SESSION_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _build_repo_with_handoff(
    tmp_path: Path, author_session_id: str, *, claimed_by: str = ""
) -> tuple:
    """Build a real repo where the handoff's add-commit -- and a follow-up
    commit -- both carry `author_session_id`'s Session-Id trailer. Returns
    `(closing_handoff_path, repo_root)`.

    `claimed_by`, when non-empty, is written into the handoff's frontmatter
    mirror (`claimed_by: <session>`) -- `resolve_claim_state` answers from
    the frontmatter mirror when no claim ledger directory exists, which is
    the case for these throwaway repos.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    closing = handoffs / "closing.md"
    claim_line = f"claimed_by: {claimed_by}\n" if claimed_by else ""
    closing.write_text(
        f"---\nsession_id: s1\npredecessor: none\n{claim_line}---\nClosing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        [
            "commit", "-m",
            f"add closing handoff\n\nSession-Id: {author_session_id}",
        ],
        repo,
    )

    further = repo / "further_work.txt"
    further.write_text("more work by the same author session\n")
    _git(["add", "further_work.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"further work by the author\n\nSession-Id: {author_session_id}",
        ],
        repo,
    )

    return closing, repo


def test_pickup_author_commits_not_attributed_TO_BE_FLIPPED_BY_C2(
    tmp_path: Path,
) -> None:
    """FLIPPED BY C2 (DR-286): claim-gated pickup attribution.

    Handoff H is authored (add-commit + follow-up commit) entirely under
    session A's Session-Id trailer. The gate is run with
    closing_session_id=B, AND B holds a live claim on the handoff (the
    ordinary pickup shape: B claimed the baton, then closes it) -- a
    pickup, runner != author, claim held by runner. DR-286: this now
    attributes A's commits to A (the gate arm "B holds a live claim ->
    attribute A").
    """
    closing, repo = _build_repo_with_handoff(
        tmp_path, author_session_id=_SESSION_A, claimed_by=_SESSION_B
    )
    add_sha = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()
    follow_up_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_B
    )

    assert result.indeterminate is False, (
        f"claim-gated pickup must resolve, not fail closed to INDETERMINATE; "
        f"notes={result.notes!r}"
    )
    assert add_sha in result.shas, (
        "DR-286: the handoff's own add-commit, authored under session A, "
        f"must be attributed when B (the claim holder) closes it; "
        f"shas={result.shas!r}"
    )
    assert follow_up_sha in result.shas, (
        "DR-286: the author's follow-up commit under session A must be "
        f"attributed when B (the claim holder) closes it; shas={result.shas!r}"
    )
    assert any(
        "pickup detected" in n and _SESSION_A in n and _SESSION_B in n
        for n in result.notes
    ), f"expected a pickup-attribution note naming both sessions; notes={result.notes!r}"


def test_pickup_no_claim_attribution_unchanged(tmp_path: Path) -> None:
    """DR-286 gate arm: no claim on the handoff -> attribution UNCHANGED.

    Same author/runner mismatch as the flipped test above, but the handoff
    carries no `claimed_by` at all. Fail-closed: A's commits must NOT be
    attributed, and (mirroring pre-C2 behaviour) the substituted runner
    session B matches 0 real commits, tripping the vacuous-match guard to
    INDETERMINATE.
    """
    closing, repo = _build_repo_with_handoff(tmp_path, author_session_id=_SESSION_A)
    add_sha = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()
    follow_up_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_B
    )

    assert result.indeterminate is True, (
        f"no-claim pickup must still fail closed to INDETERMINATE via the "
        f"vacuous-match guard; shas={result.shas!r} notes={result.notes!r}"
    )
    assert add_sha not in result.shas, (
        "no claim on the handoff: A's add-commit must NOT be attributed; "
        f"shas={result.shas!r}"
    )
    assert follow_up_sha not in result.shas, (
        "no claim on the handoff: A's follow-up commit must NOT be "
        f"attributed; shas={result.shas!r}"
    )
    assert not any("pickup detected" in n for n in result.notes), (
        f"no claim held: must not emit a pickup-attribution note; "
        f"notes={result.notes!r}"
    )


def test_pickup_third_party_claim_attribution_unchanged(tmp_path: Path) -> None:
    """DR-286 gate arm: claim held by a THIRD party (not the runner) ->
    attribution UNCHANGED.

    Handoff authored by A, gate run as B, but the claim is held by a third
    session C -- neither the author nor the runner. Fail-closed: must
    behave exactly like the no-claim case, not attribute A's commits.
    """
    _SESSION_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    closing, repo = _build_repo_with_handoff(
        tmp_path, author_session_id=_SESSION_A, claimed_by=_SESSION_C
    )
    add_sha = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()
    follow_up_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_B
    )

    assert result.indeterminate is True, (
        f"third-party-claim pickup must still fail closed to INDETERMINATE "
        f"via the vacuous-match guard; shas={result.shas!r} notes={result.notes!r}"
    )
    assert add_sha not in result.shas, (
        "claim held by a third party: A's add-commit must NOT be "
        f"attributed; shas={result.shas!r}"
    )
    assert follow_up_sha not in result.shas, (
        "claim held by a third party: A's follow-up commit must NOT be "
        f"attributed; shas={result.shas!r}"
    )
    assert not any("pickup detected" in n for n in result.notes), (
        f"claim held by a third party: must not emit a pickup-attribution "
        f"note; notes={result.notes!r}"
    )


def test_pickup_render_surfaces_attribution_disagrees(tmp_path: Path) -> None:
    """R1 (this chunk): `_render_dag_ancestry_notes` reads
    `_DagNodeAttribution.authored_session_id` / `.attribution_disagrees` and
    surfaces the detected pickup on the closing node's ancestry line, rather
    than leaving it visible only via `result.notes`'s "pickup detected"
    prose.

    Builds the same claim-gated pickup shape as
    `test_pickup_author_commits_not_attributed_TO_BE_FLIPPED_BY_C2` (handoff
    authored by session A, closed by session B who holds the claim), runs
    the real `_derive_dag_chain_set` (no stubbing), and renders its
    `node_attribution` through `_render_dag_ancestry_notes` exactly as
    `run_coverage_gate` does.
    """
    closing, repo = _build_repo_with_handoff(
        tmp_path, author_session_id=_SESSION_A, claimed_by=_SESSION_B
    )

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_B
    )
    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"

    closing_abs = str(closing.resolve())
    closing_attrib = result.node_attribution[closing_abs]
    assert closing_attrib.attribution_disagrees is True, (
        "fixture must produce a detected pickup on the closing node for "
        f"this test to be meaningful; attrib={closing_attrib!r}"
    )
    assert closing_attrib.authored_session_id == _SESSION_A

    lines = cov._render_dag_ancestry_notes(
        ordered_ancestry=result.ordered_ancestry,
        node_attribution=result.node_attribution,
        closing_abs=closing_abs,
        uncovered_shas=result.shas,
    )
    joined = "\n".join(lines)

    assert "picked up" in joined and _SESSION_A in joined, (
        f"expected the closing node's line to name the authoring session "
        f"({_SESSION_A}) as a detected pickup; rendered=\n{joined}"
    )


def test_same_session_author_runner_control_MUST_NOT_CHANGE(tmp_path: Path) -> None:
    """CONTROL -- must never change, at C2 or any other point.

    Author == runner (the ordinary, non-pickup path): the handoff is
    authored under session A, and the gate is also run with
    closing_session_id=A. Output must be byte-identical to a closing_session_id=""
    run over the same repo (the ordinary path used by every pre-existing
    test in this tree), proving the from_handoff substitution is a no-op
    when there is no pickup. This is the property that makes C1 and C2 safe
    by construction -- do not mark this test as flippable, and do not change
    it when C2 lands.
    """
    closing, repo = _build_repo_with_handoff(tmp_path, author_session_id=_SESSION_A)
    add_sha = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()
    follow_up_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result_same_session = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_A
    )
    result_no_closing_session_id = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result_same_session.indeterminate is False, (
        f"unexpected INDETERMINATE: {result_same_session.notes!r}"
    )
    assert add_sha in result_same_session.shas, (
        f"same-session (author==runner) must attribute the add-commit; "
        f"shas={result_same_session.shas!r}"
    )
    assert follow_up_sha in result_same_session.shas, (
        f"same-session (author==runner) must attribute the follow-up commit; "
        f"shas={result_same_session.shas!r}"
    )
    assert result_same_session.shas == result_no_closing_session_id.shas, (
        "author==runner must be byte-identical to the ordinary "
        "closing_session_id='' path -- the from_handoff substitution must "
        f"be a no-op here; same={result_same_session.shas!r} "
        f"empty={result_no_closing_session_id.shas!r}"
    )


def test_pickup_claim_read_failure_attribution_unchanged(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """DR-286 gate arm: the claim read itself RAISES -> attribution UNCHANGED.

    `_get_handoff_consumed_by` (coverage.py ~2236) degrades any read/parse
    exception off the claim record to a fail-closed `None` -- this is the
    security property C2's gate rests on: a false-open here would let a
    session name an arbitrary handoff via `--from-handoff` and mint a
    permanent waiver over another session's commits. No existing test in
    this file exercises that degrade path; the other two gate-arm tests
    cover "claim absent" and "claim held by a third party," not "claim read
    raised."

    A real unreadable-claim-record fixture is not available here: the DAG
    walk that establishes `closing_set` (Step 1, `dag.walk_forward`) reads
    the SAME closing handoff file from disk that the Step-3 claim-gate read
    targets, so the file must stay genuinely readable for the walk to
    succeed at all -- there is no window in which to make the file
    unreadable only for the later claim read without corrupting the earlier
    walk too (and doing so via a real OS-permission flip is not portable on
    Windows in any case). This mirrors the sibling regression suite's own
    precedent for the identical constraint
    (`test_coverage_dag_silent_fallback_guards.py::
    test_fixpoint_propagates_handoff_read_failure_to_indeterminate`), which
    monkeypatches the same leaf function for the same reason. Following that
    precedent: monkeypatch `cov._parse_handoff_consumed_by` (the leaf
    `_get_handoff_consumed_by` delegates to) to raise -- `dag.walk_forward`
    never calls this coverage.py-local function, so Step 1's real read is
    untouched; only the Step-3 claim-gate read is affected.
    """
    closing, repo = _build_repo_with_handoff(
        tmp_path, author_session_id=_SESSION_A, claimed_by=_SESSION_B
    )
    add_sha = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()
    follow_up_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    def _boom(path, **kwargs):
        raise OSError("claim record unreadable")

    monkeypatch.setattr(cov, "_parse_handoff_consumed_by", _boom)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=_SESSION_B
    )

    assert result.indeterminate is True, (
        f"claim-read failure must still fail closed to INDETERMINATE via "
        f"the vacuous-match guard; shas={result.shas!r} notes={result.notes!r}"
    )
    assert add_sha not in result.shas, (
        "claim read raised: A's add-commit must NOT be attributed; "
        f"shas={result.shas!r}"
    )
    assert follow_up_sha not in result.shas, (
        "claim read raised: A's follow-up commit must NOT be attributed; "
        f"shas={result.shas!r}"
    )
    assert not any("pickup detected" in n for n in result.notes), (
        f"claim read raised: must not emit a pickup-attribution note; "
        f"notes={result.notes!r}"
    )
    captured = capsys.readouterr()
    assert "_get_handoff_consumed_by" in captured.err and "OSError" in captured.err, (
        "the fail-closed degrade must still surface its stderr diagnostic; "
        f"stderr={captured.err!r}"
    )
