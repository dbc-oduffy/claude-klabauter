"""
coordinator_core.tests.test_coverage_dag_bulk_sweep_add_commit — regression
tests for the 2026-08-11 add-commit-authorship-is-not-a-bulk-sweep fix
(docs/plans/2026-08-11-coverage-add-commit-authorship-is-not-a.md, C1/C4).

Root cause (coordinator_core/coverage.py, _derive_dag_chain_set Step 3, prior
to this fix): a coverable node's authoring session is resolved from its
add-commit's Session-Id trailer via `--follow -M100% --diff-filter=A`. When
that add-commit is a BULK SWEEP — some other session's routine multi-file
safety/auto commit that happened to include this one handoff among many
unrelated files — the sweeping session is not evidence of authorship, but
leg (b) (the deliverable-attribution legacy-history fallback) fanned out
across every untrailered commit that session ever made anyway. Verified on
disk against chain `dlv-sat-03`: baton A's add-commit (`b8a8339a6`, subject
"safety-commit", 56 files) is Session-Id `46c0cc4c-...`, an unrelated
auto-push/DR-059 workstream session that never authored the baton — leg (b)
credited 11 of that session's commits to the chain, 10 of them untrailered.

Fix: `_add_commit_touched_file_count` gates leg (b) on the add-commit's own
touched-file count — see `_BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD`'s docstring
for the structural (not commit-subject) rationale. Leg (a) is untouched;
suppressed leg (b) commits surface on `_DagChainResult.unattributable_shas`
(C2) rather than being dropped.

Follows the fixture-building pattern established in
test_coverage_dag_deliverable_attribution.py (_init_repo / _git).
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


_SWEEPING_SESSION_ID = "46c0cc4c-e257-45a6-b1fc-b6d8d30876b3"
_DELIVERABLE_ID = "dlv-sat-03-fixture"


def test_bulk_sweep_add_commit_does_not_seed_leg_b(tmp_path: Path) -> None:
    """A node whose add-commit is a bulk sweep (many files, foreign session)
    must not have that session's untrailered commits credited via leg (b) —
    while leg (a) (Deliverable-Id match) stays fully credited, and the node
    does NOT become INDETERMINATE.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # The bulk-sweep add-commit: the closing handoff plus enough filler files
    # to exceed _BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD, authored by a session
    # unrelated to this chain (mirrors b8a8339a6's 56-file "safety-commit").
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    closing = handoffs / "closing.md"
    closing.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: none\n"
        f"deliverable_id: {_DELIVERABLE_ID}\n"
        "---\n"
        "Closing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    filler_count = cov._BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD + 5
    for i in range(filler_count):
        f = repo / f"sweep_filler_{i}.txt"
        f.write_text(f"unrelated swept file {i}\n")
        _git(["add", f.name], repo)
    _git(
        [
            "commit", "-m",
            f"safety-commit\n\nSession-Id: {_SWEEPING_SESSION_ID}",
        ],
        repo,
    )
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # Leg (b) candidate: an UNTRAILERED commit from the same sweeping
    # session — pre-fix, this would be credited via the legacy-history
    # fallback; post-fix it must NOT be, because the add-commit that seeded
    # the session resolution was a bulk sweep.
    foreign_legacy = repo / "foreign_legacy.txt"
    foreign_legacy.write_text("unrelated auto-push work\n")
    _git(["add", "foreign_legacy.txt"], repo)
    _git(
        ["commit", "-m", f"auto-push\n\nSession-Id: {_SWEEPING_SESSION_ID}"],
        repo,
    )
    foreign_legacy_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # Leg (a) control: a commit stamped with the MATCHING Deliverable-Id
    # under a wholly different session — must still be credited; the fix
    # must not touch leg (a) at all.
    other_session_id = "66666666-6666-6666-6666-666666666666"
    same_work = repo / "same_work.txt"
    same_work.write_text("real chain work, correct deliverable\n")
    _git(["add", "same_work.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"real work\n\nSession-Id: {other_session_id}\n"
            f"Deliverable-Id: {_DELIVERABLE_ID}",
        ],
        repo,
    )
    same_work_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, (
        f"a node with an unreliable add-commit must NOT become INDETERMINATE "
        f"(AC4); notes={result.notes!r}"
    )
    # The add-commit itself carries no Deliverable-Id, so it is a leg (b)
    # candidate too — also correctly suppressed by the bulk-sweep guard.
    assert add_sha not in result.shas, (
        "the bulk-sweep add-commit itself carries no Deliverable-Id and "
        f"must not be credited via the suppressed leg (b); shas={result.shas!r}"
    )
    assert foreign_legacy_sha not in result.shas, (
        "an untrailered commit from the bulk-sweep session must NOT be "
        f"credited once the seeding add-commit is judged unreliable; "
        f"shas={result.shas!r}"
    )
    assert same_work_sha in result.shas, (
        "leg (a) (matching Deliverable-Id) must remain fully credited "
        f"regardless of the add-commit's reliability; shas={result.shas!r}"
    )

    closing_attribution = result.node_attribution[str(closing.resolve())]
    assert foreign_legacy_sha not in closing_attribution.shas
    assert same_work_sha in closing_attribution.shas

    # C2: suppressed leg (b) commits are reported, not dropped.
    assert foreign_legacy_sha in result.unattributable_shas, (
        "a commit excluded by the bulk-sweep guard must surface on "
        f"unattributable_shas rather than vanish; "
        f"unattributable_shas={result.unattributable_shas!r}"
    )
    assert add_sha in result.unattributable_shas
    assert any("bulk sweep" in n or "unattributable" in n for n in result.notes), (
        f"a note must record the suppression; notes={result.notes!r}"
    )


def test_small_add_commit_below_threshold_still_seeds_leg_b(tmp_path: Path) -> None:
    """Control: an add-commit touching FEW files (below the threshold) is
    unaffected — leg (b) still fans out exactly as before the fix.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    closing = handoffs / "closing.md"
    closing.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: none\n"
        f"deliverable_id: {_DELIVERABLE_ID}\n"
        "---\n"
        "Closing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    session_id = "55555555-5555-5555-5555-555555555555"
    _git(
        ["commit", "-m", f"add closing handoff\n\nSession-Id: {session_id}"],
        repo,
    )
    add_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    legacy = repo / "legacy.txt"
    legacy.write_text("legacy untrailered work\n")
    _git(["add", "legacy.txt"], repo)
    _git(["commit", "-m", f"legacy work\n\nSession-Id: {session_id}"], repo)
    legacy_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False
    assert add_sha in result.shas
    assert legacy_sha in result.shas, (
        "a small (below-threshold) add-commit must not trip the bulk-sweep "
        f"guard; shas={result.shas!r}"
    )
    assert legacy_sha not in result.unattributable_shas


def test_bulk_sweep_ancestor_surfaces_after_closing_handoff_archived(
    tmp_path: Path,
) -> None:
    """C2 gap, reproduced end-to-end: the caller's `from_handoff` argument is
    the closing handoff's PRE-archival path (the ordinary shape a diagnostic
    or ceremony re-run holds — the fleet routinely archives a handoff right
    after its closing session ends, out from under any path recorded before
    that move). The bulk-sweep ancestor (baton A) is only discoverable by
    walking the closing handoff's `predecessor` edge — which requires reading
    the closing handoff's OWN frontmatter first.

    Pre-fix: `_derive_dag_chain_set` passed the caller's `from_handoff` path
    to `walk_forward` verbatim. Once that path no longer existed on disk
    (archived away), `walk_forward` could not read the closing node's own
    frontmatter at all, so it discovered zero edges — the ancestor walk
    collapsed to the single closing node, the bulk-sweep baton was never
    visited, and `unattributable_shas` was silently empty even though a real
    foreign-session bulk sweep was in scope. This is the exact shape of the
    2026-08-11 sat-03 live case (state/handoffs/2026-08-11-sat-03-event-
    sourced-completion-core.md, archived to archive/handoffs/2026-08/ by the
    fleet before this gate was re-run against it).

    Post-fix: the closing node's on-disk path is resolved into
    `archive/handoffs/` (flat or month-nested) before the walk, exactly as
    every ancestor EDGE target already is via `dag.resolve_target` — so the
    ancestor walk succeeds and the bulk-sweep guard's suppressed commits
    surface on `unattributable_shas` as designed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)

    baton = handoffs / "baton.md"
    baton.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: none\n"
        f"deliverable_id: {_DELIVERABLE_ID}\n"
        "---\n"
        "Baton body.\n"
    )
    _git(["add", "state/handoffs/baton.md"], repo)
    filler_count = cov._BULK_SWEEP_ADD_COMMIT_FILE_THRESHOLD + 5
    for i in range(filler_count):
        f = repo / f"sweep_filler_{i}.txt"
        f.write_text(f"unrelated swept file {i}\n")
        _git(["add", f.name], repo)
    _git(
        [
            "commit", "-m",
            f"safety-commit\n\nSession-Id: {_SWEEPING_SESSION_ID}",
        ],
        repo,
    )

    foreign_legacy = repo / "foreign_legacy.txt"
    foreign_legacy.write_text("unrelated auto-push work\n")
    _git(["add", "foreign_legacy.txt"], repo)
    _git(
        ["commit", "-m", f"auto-push\n\nSession-Id: {_SWEEPING_SESSION_ID}"],
        repo,
    )
    foreign_legacy_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\n"
        "session_id: s1\n"
        "predecessor: baton.md\n"
        f"deliverable_id: {_DELIVERABLE_ID}\n"
        "---\n"
        "Closing body.\n"
    )
    real_session_id = "77777777-7777-7777-7777-777777777777"
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        ["commit", "-m", f"add closing\n\nSession-Id: {real_session_id}"],
        repo,
    )

    # Stale reference: the path a caller would hold BEFORE the fleet's
    # post-close archival sweep — captured before the file is ever moved.
    stale_from_handoff = str(closing.resolve())

    # Simulate the fleet's routine archival: move the closing handoff out of
    # state/handoffs/ into archive/handoffs/ (flat form — a month-nested
    # subdirectory is exercised by the sat-03 live case but the flat tier is
    # sufficient to prove the resolution fallback engages).
    archive_dir = repo / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    _git(["mv", "state/handoffs/closing.md", "archive/handoffs/closing.md"], repo)
    _git(["commit", "-m", "fleet: archive closing handoff"], repo)
    assert not Path(stale_from_handoff).exists(), (
        "fixture setup error: the pre-archival path must no longer exist on "
        "disk for this test to reproduce the live-case gap"
    )

    result = cov._derive_dag_chain_set(
        stale_from_handoff, str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, (
        f"an archived closing handoff must not become INDETERMINATE; "
        f"notes={result.notes!r}"
    )
    assert len(result.ordered_ancestry) > 1, (
        "the ancestor walk must reach the bulk-sweep baton through the "
        f"archived closing handoff's predecessor edge; "
        f"ordered_ancestry={result.ordered_ancestry!r}"
    )
    assert foreign_legacy_sha in result.unattributable_shas, (
        "the bulk-sweep ancestor's foreign commit must surface on "
        "unattributable_shas even when the closing handoff argument is a "
        f"stale (since-archived) path; unattributable_shas="
        f"{result.unattributable_shas!r}"
    )
