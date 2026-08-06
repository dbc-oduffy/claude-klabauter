"""
coordinator_core.tests.test_coverage_dag_uncovered_ancestry_render — regression
tests for the DAG-mode UNCOVERED baton-ancestry disclosure.

Root cause this closes: `_derive_dag_chain_set` already computes complete
per-commit baton attribution (Step 3, segment attribution) and used to
destroy it at the `_DagChainResult` return boundary — only a flat `shas`
union survived. `run_coverage_gate`'s UNCOVERED render therefore had no way
to tell an operator WHOSE inheritance an uncovered commit was, which is what
led a real chain-terminal EM to misreport 29 of their own inherited commits
as "another session's problem" (see coverage.py's `_render_dag_ancestry_notes`
docstring for the full incident).

These tests drive the WIRED path — `run_coverage_gate` (the gate's own public
entrypoint) through to `CoverageResult.notes` (the exact strings
`coordinator_core.ops.coverage_gate` forwards unchanged onto the JSON-RPC
`notes` field, which `review-coverage-gate.py` then prints verbatim to
stderr — see that op module's `notes = list(result.notes)` assembly). A green
unit test on `_derive_dag_chain_set` alone would not catch a regression where
the attribution was computed but never threaded into the render.

Fixture shape: a two-node baton chain (ancestor -> closing), each node
contributing one bookkeeping commit (its own handoff add, under
state/handoffs/ — excluded from the verdict by the bookkeeping partition)
and one CODE commit (a source-path change under the SAME Session-Id trailer
— left uncovered, since no review-trail record is written in these tests).
Mirrors the fixture-building pattern established in
test_coverage_dag_chain_set_cross_branch.py / test_coverage_dag_deliverable_
attribution.py (_init_repo / _git).
"""

from __future__ import annotations

import json
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


_SID_ANCESTOR = "11111111-1111-1111-1111-111111111111"
_SID_CLOSING = "22222222-2222-2222-2222-222222222222"
_DLV_ANCESTOR = "dlv-ancestor-baton-abc123"
_DLV_CLOSING = "dlv-closing-baton-def456"


def _build_two_node_chain(repo: Path) -> Path:
    """Build ancestor -> closing, each contributing one handoff-authoring commit
    and one src/ commit under its own Session-Id. Returns the closing handoff's
    absolute path.

    All four are CODE: introducing `state/handoffs/<name>.md` is handoff
    authoring, the primary content this gate tracks, not ceremony exhaust (see
    coverage._handoff_authoring_shas). This fixture used to call the two handoff
    adds "bookkeeping", which was the 87578a319 regression — under that reading
    a whole DAG chain classifies as ceremony and VERDICT=COVERED fires whether
    or not any review happened. A caller needing a genuine bookkeeping commit
    adds one itself (see _commit_ceremony_note).
    """
    # Base commit first: the ancestor handoff add is a chain commit, and a trail
    # record covering it resolves `<sha>^`, which does not exist for a root
    # commit. Trailerless, so it joins no session segment and no chain.
    (repo / "README.md").write_text("base\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "base commit"], repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)

    ancestor = handoffs / "ancestor.md"
    ancestor.write_text(
        "---\n"
        "session_id: s-ancestor\n"
        "predecessor: none\n"
        f"deliverable_id: {_DLV_ANCESTOR}\n"
        "---\n"
        "Ancestor body.\n"
    )
    _git(["add", "state/handoffs/ancestor.md"], repo)
    _git(
        ["commit", "-m", f"add ancestor handoff\n\nSession-Id: {_SID_ANCESTOR}"],
        repo,
    )

    src = repo / "src"
    src.mkdir(exist_ok=True)
    ancestor_feature = src / "ancestor_feature.py"
    ancestor_feature.write_text("# ancestor work\n")
    _git(["add", "src/ancestor_feature.py"], repo)
    _git(
        [
            "commit", "-m",
            f"ancestor code work\n\nSession-Id: {_SID_ANCESTOR}",
        ],
        repo,
    )

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\n"
        "session_id: s-closing\n"
        "predecessor: ancestor.md\n"
        f"deliverable_id: {_DLV_CLOSING}\n"
        "---\n"
        "Closing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {_SID_CLOSING}"],
        repo,
    )

    closing_feature = src / "closing_feature.py"
    closing_feature.write_text("# closing work\n")
    _git(["add", "src/closing_feature.py"], repo)
    _git(
        [
            "commit", "-m",
            f"closing code work\n\nSession-Id: {_SID_CLOSING}",
        ],
        repo,
    )

    return closing.resolve()


def _commit_ceremony_note(repo: Path) -> None:
    """Add one genuine ceremony-exhaust commit to the closing node's segment.

    `tasks/` only, and it introduces no handoff — so it satisfies both legs of
    the bookkeeping partition and lands in `bookkeeping_shas`. Kept out of
    `_build_two_node_chain` so tests keying on that fixture's CODE-commit count
    are unaffected.
    """
    tasks = repo / "tasks"
    tasks.mkdir(exist_ok=True)
    (tasks / "ceremony-note.md").write_text("orphan sweep note\n")
    _git(["add", "tasks/ceremony-note.md"], repo)
    _git(
        ["commit", "-m", f"session.boot_sweep: orphan note\n\nSession-Id: {_SID_CLOSING}"],
        repo,
    )


def test_uncovered_notes_carry_baton_ancestry_and_per_commit_tags(
    tmp_path: Path,
) -> None:
    """DAG-mode UNCOVERED must render: the inheritance sentence, the ancestry
    chain (both nodes, closing marked), and each uncovered CODE commit tagged
    with its originating baton label — with the frozen verdict line unchanged.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    closing_path = _build_two_node_chain(repo)

    result = cov.run_coverage_gate(
        from_handoff=str(closing_path),
        repo_root=str(repo),
        closing_session_id="",
    )

    assert result.verdict == "WARN", (
        f"expected WARN (no trail records written); got {result.verdict}, "
        f"notes={result.notes!r}"
    )
    assert result.verdict_line.startswith("range=dag:closing.md ")
    assert result.verdict_line.endswith("VERDICT=WARN")
    assert "chain_commits=" in result.verdict_line
    assert "covered=" in result.verdict_line
    assert f"uncovered={len(result.uncovered_shas)}" in result.verdict_line

    # AC1/AC2 — attribution threaded through CoverageResult, not just notes.
    assert len(result.dag_ordered_ancestry) == 2
    assert len(result.dag_node_attribution) == 2
    ancestor_attrib = result.dag_node_attribution[str(repo / "state" / "handoffs" / "ancestor.md")]
    closing_attrib = result.dag_node_attribution[str(closing_path)]
    assert ancestor_attrib.deliverable_id == _DLV_ANCESTOR
    assert closing_attrib.deliverable_id == _DLV_CLOSING

    joined_notes = "\n".join(result.notes)
    assert "inheritance" in joined_notes.lower()
    assert "baton ancestry" in joined_notes.lower()
    assert "ancestor" in joined_notes  # handoff name rendered
    assert "closing" in joined_notes
    assert "<- closing (you)" in joined_notes
    assert _DLV_ANCESTOR in joined_notes
    assert _DLV_CLOSING in joined_notes
    assert "uncovered, by originating baton:" in joined_notes

    # Both CODE commits (not the bookkeeping handoff-add commits) must be
    # present, each tagged with a "[<label>]  <short-sha>" line.
    for sha in result.uncovered_shas:
        assert f"  {sha[:9]}" in joined_notes, (
            f"uncovered sha {sha} not tagged with a baton label in notes: {result.notes!r}"
        )


def test_bookkeeping_note_defaults_to_count_not_raw_repr(tmp_path: Path) -> None:
    """AC5 — the bookkeeping-partition note defaults to a count, not a raw
    Python repr of the full sha list; verbose=True opts into the full list.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    closing_path = _build_two_node_chain(repo)
    _commit_ceremony_note(repo)

    quiet = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
    )
    quiet_bookkeeping_notes = [n for n in quiet.notes if "ceremony" in n and "bookkeeping" in n]
    assert quiet_bookkeeping_notes, f"expected a bookkeeping note; notes={quiet.notes!r}"
    assert not any("[" in n and "]" in n and "'" in n for n in quiet_bookkeeping_notes), (
        f"default (non-verbose) note should not embed a raw list repr: {quiet_bookkeeping_notes!r}"
    )
    assert any(str(len(quiet.bookkeeping_shas)) in n for n in quiet_bookkeeping_notes)

    loud = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
        verbose=True,
    )
    loud_bookkeeping_notes = [n for n in loud.notes if "ceremony" in n and "bookkeeping" in n]
    assert loud_bookkeeping_notes
    assert any(repr(loud.bookkeeping_shas) in n for n in loud_bookkeeping_notes), (
        f"verbose=True should embed the full sha list: {loud_bookkeeping_notes!r}"
    )


def test_uncovered_notes_carry_attribution_disclosure(tmp_path: Path) -> None:
    """AC8 — DAG-mode UNCOVERED notes disclose the trailer-derived attribution
    limit (§ "Honest accounting" in
    docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md):
    incomplete trailer coverage, plan-file-staging over-inclusion, and
    range-based (not per-SHA) crediting. This is the assembly point every
    DAG-mode UNCOVERED consumer inherits from — see that function's
    docstring.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    closing_path = _build_two_node_chain(repo)

    result = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
    )
    assert result.verdict == "WARN"
    joined_notes = "\n".join(result.notes)
    assert "trailer-derived" in joined_notes
    assert "range-based" in joined_notes


def test_covered_notes_do_not_carry_attribution_disclosure(tmp_path: Path) -> None:
    """A limit disclosed on every run trains people to skip it — the
    disclosure must appear ONLY on DAG-mode UNCOVERED, never on COVERED.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    closing_path = _build_two_node_chain(repo)

    uncovered = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
    ).uncovered_shas
    # Four CODE commits: two handoff-authoring adds plus two src/ commits. The
    # adds are content, not ceremony — see _build_two_node_chain's docstring.
    assert len(uncovered) == 4, f"fixture assumption changed: uncovered={uncovered!r}"

    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    for i, sha in enumerate(uncovered):
        record = {
            "sha_range": f"{sha}^..{sha}",
            "reviewer": "code-reviewer",
            # scope=None (not "session"/"chain"/"workstream-close-auto") so
            # _narrow_foreign_session_scope's foreign-session stripping never
            # engages — this test wants a plain COVERED, not a foreign-scope
            # crediting scenario (that is C4b's surface, not C5's).
            "scope": None,
            "scope_kind": "diff",
            "verdict": "ok",
            "diff_loc": 1,
            "session_id": "00000000-0000-0000-0000-000000000001",
        }
        (trail_dir / f"record_{i:02d}.json").write_text(json.dumps(record), encoding="utf-8")
    _git(["add", "state/review-trail"], repo)
    _git(
        ["commit", "-m", f"add review-trail records\n\nSession-Id: {_SID_CLOSING}"],
        repo,
    )

    result = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
    )
    assert result.verdict == "COVERED", (
        f"expected COVERED with a trail record spanning the whole chain; "
        f"got {result.verdict}, uncovered={result.uncovered_shas!r}, notes={result.notes!r}"
    )
    joined_notes = "\n".join(result.notes)
    assert "trailer-derived" not in joined_notes
    assert "range-based" not in joined_notes


def test_verdict_line_byte_identical_across_verbosity(tmp_path: Path) -> None:
    """AC4 — the frozen verdict line must not change shape between the
    default and verbose renders; only `notes` differs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    closing_path = _build_two_node_chain(repo)

    quiet = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
    )
    loud = cov.run_coverage_gate(
        from_handoff=str(closing_path), repo_root=str(repo), closing_session_id="",
        verbose=True,
    )
    assert quiet.verdict_line == loud.verdict_line
