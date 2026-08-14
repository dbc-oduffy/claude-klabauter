"""
coordinator_core.tests.test_coverage_bookkeeping_partition — regression tests for
the bookkeeping-vs-code uncovered-commit partition in coordinator_core/coverage.py.

Root cause this closes: the review-coverage gate is an ORACLE, not a lock (the
engine disclaims enforcement — see ops/ceremony/tail_ops.py:698), yet it used to
return VERDICT=UNCOVERED forever on any workstream that ran
`/workstream-complete`, because the ceremony necessarily authors its own
bookkeeping commits (completion entry, review-trail record, shipped_in stamp,
boot sweep, pickup-assemble claim) AFTER the trail record that would cover
them. Neither a "pickup-assemble apply:" handoff-claim commit nor a
"session.boot_sweep:" orphan-note commit is code a reviewer could open — but
both structurally can never be covered by any trail record persisted before
them.

Fix: partition the uncovered set into `code` and `bookkeeping` on TWO legs, and
key the verdict on the code partition only. The bookkeeping partition is still
surfaced via CoverageResult.bookkeeping_shas + a notes entry, never silently
dropped.

  1. Touched path — every path under state/, archive/, tasks/, cross-repo/
     (coverage._BOOKKEEPING_PATH_PREFIXES).
  2. Change type — the commit introduces no file under state/handoffs/
     (coverage._handoff_authoring_shas).

Leg 2 exists because leg 1 alone made the gate vacuous (regression 87578a319,
tests H/I below): a handoff-authoring commit writes `state/handoffs/<name>.md`
and nothing else, so every DAG chain classified 100% bookkeeping and
VERDICT=COVERED fired regardless of review status. Change type is what separates
authoring a handoff (content) from stamping `shipped_in` on one (exhaust) —
they touch the same file, so no path rule can do it.

Anti-exploit property (tested explicitly, test C below): a commit that touches
BOTH a bookkeeping path and any other (non-bookkeeping) path classifies as
CODE, fail-closed — see coverage._classify_bookkeeping_shas.

Spec backlink: cross-repo/inbox dispatch, "review-coverage gate: partition
uncovered-by-bookkeeping" (DoE-claude, 2026-07-26).
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


def _write_boot_sweep_note(repo: Path) -> None:
    """Commit a `session.boot_sweep:` ceremony note — a state/-prefixed commit
    that is unambiguously exhaust.

    Deliberately NOT `state/handoffs/<name>.md`: introducing a file there is
    handoff AUTHORING, the primary content the gate tracks, and classifies as
    CODE (see coverage._handoff_authoring_shas). The real boot sweep writes
    `state/handoff-tracker.md` and `tasks/orphan-sweep-notes.md`, never a
    handoff — the original fixture's `state/handoffs/note.md` path encoded a
    commit shape that does not occur, and asserting bookkeeping on it is
    indistinguishable from the 87578a319 regression that made every DAG chain
    vacuously COVERED.
    """
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "handoff-tracker.md").write_text("orphan note\n")
    _git(["add", "state/handoff-tracker.md"], repo)
    _git(["commit", "-m", "session.boot_sweep: orphan note"], repo)


def _base_repo(tmp_path: Path) -> Path:
    """A minimal repo with one base commit — the flat-mode range base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("base\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "base commit"], repo)
    return repo


def test_bookkeeping_only_chain_is_covered(tmp_path: Path) -> None:
    """A. A chain whose only unreviewed commits touch state/ and tasks/ only
    returns VERDICT=COVERED — no review-trail record needed, since neither
    commit is code a reviewer could open.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    _write_boot_sweep_note(repo)

    tasks_dir = repo / "tasks" / "abc123"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "todo.md").write_text("todo\n")
    _git(["add", "tasks/abc123/todo.md"], repo)
    _git(["commit", "-m", "pickup-assemble apply: claim memo"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "COVERED", (
        f"expected COVERED for a bookkeeping-only uncovered chain; "
        f"verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert result.uncovered_shas == []
    assert len(result.bookkeeping_shas) == 2, (
        f"expected both bookkeeping commits surfaced, not dropped; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_real_source_chain_is_uncovered(tmp_path: Path) -> None:
    """B. A chain with a genuinely unreviewed commit touching real source still
    returns VERDICT=WARN (C10: below-threshold, never COVERED) — the
    partition must not blanket-launder every uncovered commit to COVERED.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    (repo / "app.py").write_text("print('hello')\n")
    _git(["add", "app.py"], repo)
    _git(["commit", "-m", "add app.py"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"expected WARN for a chain with a genuinely unreviewed source "
        f"commit; verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert len(result.uncovered_shas) == 1
    assert result.bookkeeping_shas == []


def test_mixed_commit_classifies_as_code_fail_closed(tmp_path: Path) -> None:
    """C. Anti-exploit case: a single commit touching BOTH a bookkeeping path
    (state/foo.md) AND a source file must classify as CODE, not bookkeeping —
    fail-closed. If this classified as bookkeeping, a workstream could smuggle
    an unreviewed source change past the gate by pairing it with any
    state/-prefixed edit in the same commit.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "foo.md").write_text("bookkeeping\n")
    (repo / "app.py").write_text("print('mixed')\n")
    _git(["add", "state/foo.md", "app.py"], repo)
    _git(["commit", "-m", "mixed commit: bookkeeping + source"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"a mixed bookkeeping+source commit must classify as CODE (fail-closed) "
        f"and keep the verdict WARN; verdict_line={result.verdict_line!r}"
    )
    assert len(result.uncovered_shas) == 1
    assert result.bookkeeping_shas == [], (
        f"the mixed commit must NOT be classified as bookkeeping; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_multi_commit_chain_mixes_bookkeeping_and_code(tmp_path: Path) -> None:
    """D. The real-world shape this fix targets: a chain with SEVERAL
    bookkeeping-only commits coexisting with a genuinely unreviewed source
    commit. Must classify per-commit correctly within one batched
    `_commit_touched_paths` call (exercises >1 uncached sha alongside a code
    sha, unlike A/B/C which never mix >1 sha types in one batch), and the
    frozen verdict-line arithmetic must hold: covered + uncovered ==
    chain_commits, with bookkeeping commits counted as covered.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    _write_boot_sweep_note(repo)

    tasks_dir = repo / "tasks" / "abc123"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "todo.md").write_text("todo\n")
    _git(["add", "tasks/abc123/todo.md"], repo)
    _git(["commit", "-m", "pickup-assemble apply: claim memo"], repo)

    (repo / "app.py").write_text("print('hello')\n")
    _git(["add", "app.py"], repo)
    _git(["commit", "-m", "add app.py"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"a mixed chain with one genuinely unreviewed source commit must stay "
        f"WARN; verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert len(result.uncovered_shas) == 1, (
        f"expected exactly the app.py commit uncovered; "
        f"uncovered_shas={result.uncovered_shas!r}"
    )
    assert len(result.bookkeeping_shas) == 2, (
        f"expected both bookkeeping commits classified and surfaced; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )
    assert result.chain_commits == 3
    assert result.covered + result.uncovered == result.chain_commits, (
        "documented arithmetic (covered + uncovered == chain_commits, "
        "bookkeeping counted as covered) must hold"
    )
    assert f"covered={result.covered}" in result.verdict_line
    assert f"uncovered={result.uncovered}" in result.verdict_line


def test_cross_repo_only_commit_is_covered(tmp_path: Path) -> None:
    """F. A commit touching ONLY cross-repo/ (memo traffic — inbox/archive) is
    bookkeeping, same as state/archive/tasks: filing or actioning a memo is
    ceremony bookkeeping, not code a reviewer could open. Regression for a
    real false-positive — a pure `git mv` of two actioned memos into
    cross-repo/archive/ (DoE-claude commit 17421262) was flagged UNCOVERED
    solely because cross-repo/ was absent from _BOOKKEEPING_PATH_PREFIXES.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    inbox_dir = repo / "cross-repo" / "archive"
    inbox_dir.mkdir(parents=True)
    (inbox_dir / "memo.md").write_text("actioned memo\n")
    _git(["add", "cross-repo/archive/memo.md"], repo)
    _git(["commit", "-m", "cross-repo: archive actioned memo"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "COVERED", (
        f"expected COVERED for a cross-repo/-only uncovered commit; "
        f"verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert result.uncovered_shas == []
    assert len(result.bookkeeping_shas) == 1


def test_cross_repo_plus_code_commit_classifies_as_code(tmp_path: Path) -> None:
    """G. Anti-exploit case for the new prefix: a commit touching BOTH
    cross-repo/ AND a real code path (coordinator/skills/foo/SKILL.md) must
    classify as CODE, fail-closed — the all-must-match rule is not weakened
    by adding cross-repo/ to the prefix tuple.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    cross_repo_dir = repo / "cross-repo" / "inbox"
    cross_repo_dir.mkdir(parents=True)
    (cross_repo_dir / "memo.md").write_text("incoming memo\n")

    skill_dir = repo / "coordinator" / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# foo\n")

    _git(["add", "cross-repo/inbox/memo.md", "coordinator/skills/foo/SKILL.md"], repo)
    _git(["commit", "-m", "mixed commit: memo + skill"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"a mixed cross-repo/+code commit must classify as CODE (fail-closed) "
        f"and keep the verdict WARN; verdict_line={result.verdict_line!r}"
    )
    assert len(result.uncovered_shas) == 1
    assert result.bookkeeping_shas == [], (
        f"the mixed commit must NOT be classified as bookkeeping; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_merge_commit_in_uncovered_set_classifies_as_code(tmp_path: Path) -> None:
    """E. A merge commit in the uncovered set must classify as CODE despite
    resolving with ZERO touched paths (`git log --no-walk --name-only` shows no
    diff for a merge commit without -m/-c). Pins the `paths and all(...)`
    empty-set guard in _classify_bookkeeping_shas — tests A/B/C would still
    pass if that guard were accidentally inverted (none of them produce a
    zero-path commit), so this test must fail if it is.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    _git(["checkout", "-b", "side"], repo)
    (repo / "side.txt").write_text("side branch\n")
    _git(["add", "side.txt"], repo)
    _git(["commit", "-m", "add side.txt"], repo)

    _git(["checkout", "main"], repo)
    (repo / "main.txt").write_text("main branch\n")
    _git(["add", "main.txt"], repo)
    _git(["commit", "-m", "add main.txt"], repo)

    _git(["merge", "--no-ff", "-m", "merge side into main", "side"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"a merge commit in the uncovered set must classify as CODE, keeping "
        f"the verdict WARN; verdict_line={result.verdict_line!r}"
    )
    assert result.bookkeeping_shas == [], (
        f"a merge commit resolving with zero touched paths must NOT be "
        f"classified as bookkeeping (fail-closed); "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_handoff_authoring_commit_classifies_as_code(tmp_path: Path) -> None:
    """H. The 87578a319 regression, pinned. A commit that INTRODUCES
    `state/handoffs/<name>.md` and nothing else is handoff authoring — the
    primary content the DAG coverage gate exists to track, carrying the
    `Session-Id` trailer the whole attribution chain hangs off. It must classify
    as CODE despite every touched path sitting under a bookkeeping prefix.

    Without this, every DAG chain (whose commits are exactly this shape)
    classified 100% bookkeeping, `uncovered_shas` went permanently empty, and
    VERDICT=COVERED fired whether or not any review had happened — the gate
    stopped gating. See coverage._handoff_authoring_shas.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "2026-08-01-work.md").write_text("---\nstatus: active\n---\nbody\n")
    _git(["add", "state/handoffs/2026-08-01-work.md"], repo)
    _git(
        ["commit", "-m", "baton-assemble apply: handoff 2026-08-01-work.md"],
        repo,
    )

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN", (
        f"a handoff-authoring commit must classify as CODE and keep the verdict "
        f"WARN; verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert len(result.uncovered_shas) == 1
    assert result.bookkeeping_shas == [], (
        f"the handoff-authoring commit must NOT be classified as bookkeeping; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_shipped_in_stamp_on_existing_handoff_is_bookkeeping(tmp_path: Path) -> None:
    """I. The other side of H, and the property 87578a319 must keep. A commit
    that MUTATES an already-authored `state/handoffs/<name>.md` — the
    `shipped_in` stamp, the pickup-assemble claim — is ceremony exhaust that
    necessarily postdates the trail record covering the real work, and stays
    bookkeeping.

    The discriminator between H and I is change type, not path: both commits
    touch the same file. A fix that reclassified the whole `state/handoffs/`
    corpus as content would pass H and fail here, reintroducing the permanent
    false-UNCOVERED tail 87578a319 removed.
    """
    repo = _base_repo(tmp_path)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    handoff = handoffs / "2026-08-01-work.md"
    handoff.write_text("---\nstatus: active\n---\nbody\n")
    _git(["add", "state/handoffs/2026-08-01-work.md"], repo)
    _git(["commit", "-m", "baton-assemble apply: handoff"], repo)

    # Range base is the authoring commit, so only the stamp is in the chain.
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    handoff.write_text("---\nstatus: shipped\nshipped_in: deadbeef\n---\nbody\n")
    _git(["add", "state/handoffs/2026-08-01-work.md"], repo)
    _git(["commit", "-m", "ceremony: stamp shipped_in on consumed handoff(s)"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "COVERED", (
        f"a shipped_in-stamp commit mutating an existing handoff must stay "
        f"bookkeeping; verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert result.uncovered_shas == []
    assert len(result.bookkeeping_shas) == 1, (
        f"expected the stamp commit surfaced as bookkeeping; "
        f"bookkeeping_shas={result.bookkeeping_shas!r}"
    )


def test_handoff_authoring_commit_emits_change_type_note_not_mixed_note(
    tmp_path: Path,
) -> None:
    """J. AC6 fix, positive case. A handoff-authoring-only commit (test H's
    shape: introduces state/handoffs/<name>.md, touches nothing else) falls to
    CODE via the change-type leg, with an EMPTY `other_paths`. It must emit the
    dedicated change-type note (naming the handoff-authoring rule and the
    introduced path), never the mixed-commit note — the mixed-commit note's
    "AND non-bookkeeping path(s) ()" clause is false and self-contradictory for
    a commit with no non-bookkeeping paths at all.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "2026-08-01-work.md").write_text("---\nstatus: active\n---\nbody\n")
    _git(["add", "state/handoffs/2026-08-01-work.md"], repo)
    _git(
        ["commit", "-m", "baton-assemble apply: handoff 2026-08-01-work.md"],
        repo,
    )
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN"
    change_type_notes = [
        n
        for n in result.notes
        if n.startswith("coverage: ") and sha in n and "handoff-authoring rule" in n
    ]
    assert len(change_type_notes) == 1, (
        f"expected exactly one change-type note for {sha}; notes={result.notes!r}"
    )
    note = change_type_notes[0]
    assert "state/handoffs/2026-08-01-work.md" in note
    assert "mixed-commit rule" not in note
    assert "AND non-bookkeeping path(s) ()" not in note, (
        f"the change-type note must never render the empty-other_paths mixed "
        f"clause; note={note!r}"
    )
    assert not any(
        n.startswith("coverage: ") and sha in n and "mixed-commit rule" in n
        for n in result.notes
    ), f"a handoff-authoring-only commit must not also emit the mixed-commit note; notes={result.notes!r}"


def test_genuine_mixed_commit_note_wording_unchanged(tmp_path: Path) -> None:
    """K. AC6 fix, frozen-render regression. A genuine mixed commit (test C's
    shape: state/foo.md + app.py, non-empty on both sides of the partition)
    must still emit the original mixed-commit note, wording byte-identical to
    before this fix — that note is correct and frozen; only the change-type
    leg (test J) gets new behaviour.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "foo.md").write_text("bookkeeping\n")
    (repo / "app.py").write_text("print('mixed')\n")
    _git(["add", "state/foo.md", "app.py"], repo)
    _git(["commit", "-m", "mixed commit: bookkeeping + source"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN"
    expected_note = (
        "coverage: "
        f"{sha} touches ceremony bookkeeping path(s) (state/foo.md) "
        f"AND non-bookkeeping path(s) (app.py) — classified "
        "CODE under the fail-closed mixed-commit rule (a commit must touch "
        "ONLY bookkeeping paths to be excluded from the verdict)."
    )
    assert expected_note in result.notes, (
        f"expected frozen mixed-commit note wording; notes={result.notes!r}"
    )


def test_planning_plus_bookkeeping_commit_emits_planning_note_not_mixed_note(
    tmp_path: Path,
) -> None:
    """L. AC6 fix, third leg. A commit mixing a genuine planning-artifact path
    (docs/plans/) with a bookkeeping path (state/foo.md) classifies PLANNING
    (see test R's `_classify_bookkeeping_shas` shape), but the AC6 per-sha
    note loop re-derives bk_paths/other_paths from path prefixes alone —
    docs/plans/ is not a bookkeeping prefix, so a naive re-derivation would
    put the planning path in `other_paths` and render the mixed-commit
    sentence for a sha the mixed-commit rule never touched. Must instead
    check `planning_set` first and emit the dedicated PLANNING note, never
    the mixed-commit note.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "foo.md").write_text("bookkeeping\n")
    _git(["add", "docs/plans/2026-08-05-example.md", "state/foo.md"], repo)
    _git(["commit", "-m", "author plan and touch bookkeeping"], repo)
    sha = _rev(repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    planning_notes = [
        n
        for n in result.notes
        if n.startswith("coverage: ") and sha in n and "classified PLANNING" in n
    ]
    assert len(planning_notes) == 1, (
        f"expected exactly one PLANNING per-sha note for {sha}; notes={result.notes!r}"
    )
    note = planning_notes[0]
    assert "docs/plans/2026-08-05-example.md" in note
    assert "state/foo.md" in note
    assert "plan review, not a code review" in note
    assert not any(
        n.startswith("coverage: ") and sha in n and "AND non-bookkeeping path(s)" in n
        for n in result.notes
    ), f"a planning+bookkeeping commit must not emit the mixed-commit note; notes={result.notes!r}"


def _classify(repo: Path, shas: List[str]) -> tuple:
    """Call cov._classify_bookkeeping_shas directly with a fresh cache.

    Used by the PLANNING-class tests below: run_coverage_gate deliberately does
    NOT subtract planning_set from uncovered_shas (see the negative-spec
    comment at its call site), so the three-way split is only observable by
    calling the partition function directly.
    """
    return cov._classify_bookkeeping_shas(shas, str(repo), {})


def _rev(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def test_planning_only_commit_classifies_planning(tmp_path: Path) -> None:
    """M. Composed rule happy path: a docs/plans/-only commit classifies
    PLANNING, disjoint from both CODE and EXHAUST.
    """
    repo = _base_repo(tmp_path)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    _git(["add", "docs/plans/2026-08-05-example.md"], repo)
    _git(["commit", "-m", "author plan"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha in planning_set, (
        f"expected a docs/plans/-only commit classified PLANNING; "
        f"planning_set={planning_set!r} note={note!r}"
    )
    assert sha not in exhaust_set
    assert exhaust_set.isdisjoint(planning_set)


@pytest.mark.parametrize(
    "prefix,relpath",
    [
        ("docs/research/", "docs/research/2026-08-05-example.md"),
        ("docs/problems/", "docs/problems/2026-08-05-example.md"),
    ],
)
def test_each_new_planning_prefix_classifies_planning(
    tmp_path: Path, prefix: str, relpath: str
) -> None:
    """N. Each newly-added planning-artifact prefix (docs/research/,
    docs/problems/) classifies PLANNING on its own, not just docs/plans/.
    """
    repo = _base_repo(tmp_path)
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# artifact\n")
    _git(["add", relpath], repo)
    _git(["commit", "-m", f"author {prefix} artifact"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha in planning_set, (
        f"expected {prefix}-only commit classified PLANNING; "
        f"planning_set={planning_set!r} note={note!r}"
    )
    assert sha not in exhaust_set


@pytest.mark.parametrize(
    "prefix,relpath",
    [
        ("docs/decisions/", "docs/decisions/DR-999-example.md"),
        ("docs/reference/", "docs/reference/example.md"),
        ("docs/wiki/", "docs/wiki/example.md"),
    ],
)
def test_doctrine_prefixes_stay_code_not_planning(
    tmp_path: Path, prefix: str, relpath: str
) -> None:
    """O. EM's 2026-08-06 ruling, pinned: docs/decisions/, docs/reference/,
    and docs/wiki/ are doctrine/reference prose, deliberately EXCLUDED from
    _PLANNING_ARTIFACT_PATH_PREFIXES — each classifies CODE (neither PLANNING
    nor EXHAUST), same as any other reviewable source. The easiest thing to
    regress silently per the chunk brief.
    """
    repo = _base_repo(tmp_path)
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# doctrine\n")
    _git(["add", relpath], repo)
    _git(["commit", "-m", f"author {prefix} doctrine doc"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha not in planning_set, (
        f"expected {prefix}-only commit to stay CODE, not PLANNING; "
        f"planning_set={planning_set!r} note={note!r}"
    )
    assert sha not in exhaust_set


def test_exhaust_wins_over_planning_on_overlap(tmp_path: Path) -> None:
    """P. HARD REQUIREMENT 1 (C1 measured 44 live commits affected). A commit
    whose every touched path is under _BOOKKEEPING_PATH_PREFIXES —
    state/plan-sidecars/ included, since that path already starts with the
    bookkeeping prefix "state/" — must classify EXHAUST, never PLANNING, even
    though state/plan-sidecars/ also matches a planning-artifact prefix.
    EXHAUST wins on overlap by construction: planning_set is only reachable
    when >=1 touched path lies OUTSIDE the bookkeeping prefixes.
    """
    repo = _base_repo(tmp_path)
    sidecars_dir = repo / "state" / "plan-sidecars"
    sidecars_dir.mkdir(parents=True)
    (sidecars_dir / "example.C1.md").write_text("---\nstatus: complete\n---\n")
    _git(["add", "state/plan-sidecars/example.C1.md"], repo)
    _git(["commit", "-m", "sidecar: mark C1 complete"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha in exhaust_set, (
        f"expected a state/plan-sidecars/-only commit to classify EXHAUST "
        f"(bookkeeping-prefix overlap wins), not fall through to PLANNING or "
        f"CODE; exhaust_set={exhaust_set!r} planning_set={planning_set!r} "
        f"note={note!r}"
    )
    assert sha not in planning_set, (
        f"a bookkeeping-only commit must never reach the PLANNING branch even "
        f"though state/plan-sidecars/ also matches a planning-artifact prefix; "
        f"planning_set={planning_set!r}"
    )


def test_planning_plus_handoff_authoring_classifies_code(tmp_path: Path) -> None:
    """Q. HARD REQUIREMENT 2 (C1 measured 52 live commits affected). A commit
    that touches BOTH docs/plans/ and introduces state/handoffs/<name>.md must
    classify CODE, not PLANNING — the _handoff_authoring_shas change-type leg
    applies to the PLANNING predicate too. Omitting this leg reopens
    87578a319's vacuity through the new door: it would let a handoff-
    introducing commit escape review by pairing it with any docs/plans/ edit
    in the same commit.
    """
    repo = _base_repo(tmp_path)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    handoffs_dir = repo / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "2026-08-06-work.md").write_text(
        "---\nstatus: active\n---\nbody\n"
    )
    _git(
        [
            "add",
            "docs/plans/2026-08-05-example.md",
            "state/handoffs/2026-08-06-work.md",
        ],
        repo,
    )
    _git(["commit", "-m", "author plan and introduce handoff"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha not in planning_set, (
        f"a commit introducing a handoff alongside a docs/plans/ edit must "
        f"classify CODE, not PLANNING; planning_set={planning_set!r} "
        f"note={note!r}"
    )
    assert sha not in exhaust_set


def test_planning_and_bookkeeping_mix_stays_planning(tmp_path: Path) -> None:
    """R. A commit mixing a genuine planning-artifact path (docs/plans/) with
    a bookkeeping path (state/foo.md, NOT plan-sidecars) still classifies
    PLANNING under the composed rule (b): every touched path is either
    planning-artifact or bookkeeping. This is the intended coexistence case
    (a plan commit that also touches a ceremony-bookkeeping path), distinct
    from test P where every path is bookkeeping-only.
    """
    repo = _base_repo(tmp_path)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "foo.md").write_text("bookkeeping\n")
    _git(["add", "docs/plans/2026-08-05-example.md", "state/foo.md"], repo)
    _git(["commit", "-m", "author plan and touch bookkeeping"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha in planning_set, (
        f"expected the plan+bookkeeping mix to classify PLANNING; "
        f"planning_set={planning_set!r} exhaust_set={exhaust_set!r} "
        f"note={note!r}"
    )
    assert sha not in exhaust_set


def test_planning_plus_code_commit_classifies_code(tmp_path: Path) -> None:
    """S. Mixed-commit invariant carried to the third class: a commit touching
    both a planning-artifact path AND a genuine code path classifies CODE,
    fail-closed — condition (b) of the composed rule (every touched path must
    be planning-artifact or bookkeeping) is violated by the app.py path.
    """
    repo = _base_repo(tmp_path)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    (repo / "app.py").write_text("print('mixed')\n")
    _git(["add", "docs/plans/2026-08-05-example.md", "app.py"], repo)
    _git(["commit", "-m", "mixed commit: plan + code"], repo)
    sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(repo, [sha])

    assert sha not in planning_set, (
        f"a plan+code mixed commit must classify CODE, not PLANNING; "
        f"planning_set={planning_set!r} note={note!r}"
    )
    assert sha not in exhaust_set


def test_planning_set_disjoint_from_exhaust_set_across_mixed_batch(
    tmp_path: Path,
) -> None:
    """T. planning_set and exhaust_set are disjoint across a batch mixing both
    classes in one call, not just pairwise in isolated single-sha calls.
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    _write_boot_sweep_note(repo)
    exhaust_sha = _rev(repo)

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-08-05-example.md").write_text("# plan\n")
    _git(["add", "docs/plans/2026-08-05-example.md"], repo)
    _git(["commit", "-m", "author plan"], repo)
    planning_sha = _rev(repo)

    exhaust_set, planning_set, note = _classify(
        repo, [exhaust_sha, planning_sha]
    )

    assert exhaust_sha in exhaust_set
    assert planning_sha in planning_set
    assert exhaust_set.isdisjoint(planning_set), (
        f"exhaust_set and planning_set must be disjoint; "
        f"exhaust_set={exhaust_set!r} planning_set={planning_set!r} "
        f"note={note!r}"
    )
    assert base_sha not in exhaust_set and base_sha not in planning_set


def test_no_note_ever_renders_empty_path_list_on_either_side(tmp_path: Path) -> None:
    """L. Pinning invariant the memo asked for. In a chain mixing a genuine
    mixed commit AND a handoff-authoring-only commit, no AC6 note — mixed or
    change-type — ever renders an empty path list in either of its
    parenthesized path groups. This is exactly the defect being fixed: the old
    single-predicate selection let the change-type-leg commit satisfy the
    mixed-commit note's condition and render "non-bookkeeping path(s) ()".
    """
    repo = _base_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "2026-08-01-work.md").write_text("---\nstatus: active\n---\nbody\n")
    _git(["add", "state/handoffs/2026-08-01-work.md"], repo)
    _git(["commit", "-m", "baton-assemble apply: handoff 2026-08-01-work.md"], repo)

    state_dir = repo / "state"
    (state_dir / "foo.md").write_text("bookkeeping\n")
    (repo / "app.py").write_text("print('mixed')\n")
    _git(["add", "state/foo.md", "app.py"], repo)
    _git(["commit", "-m", "mixed commit: bookkeeping + source"], repo)

    result = cov.run_coverage_gate(
        range_arg=f"{base_sha}..HEAD", repo_root=str(repo)
    )

    assert result.verdict == "WARN"
    ac6_notes = [
        n
        for n in result.notes
        if n.startswith("coverage: ") and ("bookkeeping path(s) (" in n)
    ]
    assert len(ac6_notes) == 2, f"expected one note per commit; notes={result.notes!r}"
    for n in ac6_notes:
        assert "path(s) ()" not in n, (
            f"a note rendered an empty path list on one side; note={n!r}"
        )
