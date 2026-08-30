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

K-001 note (state/kill-ledger.md): `run_coverage_gate` and its
COVERED/WARN/INDETERMINATE verdict were removed under kill-ledger entry
K-001. Every test here that drove the verdict end-to-end (via
`cov.run_coverage_gate`), including its notes-wording assertions, was
deleted as dead code covering a removed function — that formatting lived
inside `run_coverage_gate` itself, not in any surviving helper. The tests
that remain call `cov._classify_bookkeeping_shas` directly; that helper
survives K-001 for its own reason — it is unrelated to DAG-mode chain
derivation.

2026-08-19 correction (state/kill-ledger.md, DAG-fixpoint cut orphaned by
K-007): the parenthetical above previously cited `_derive_dag_chain_set`'s
"live consumer, `review_brightline_gate.py::_compute_chain_oracle`" as the
reason this module stayed load-bearing. That was already stale when this
correction was written — `_compute_chain_oracle` was itself removed by
K-007, and the measured liveness walk for this cut found zero production
call sites for `_derive_dag_chain_set`. `_classify_bookkeeping_shas`'s real
live consumer is elsewhere in this module; it has no dependency on DAG-mode
chain derivation and this file needed no code change for the K-007-orphaned
cut.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
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


