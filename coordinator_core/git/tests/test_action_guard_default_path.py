"""C2: the default `commit_paths` route (no `restrict_to_session`) consults
the sweeping-pathspec predicate via `action_guard.assert_pathspec_shape_
permitted` -- the narrowed, ownership-leg-free entry point authored for this
chunk. See `state/dispatch-briefs/2026-08-30-the-op-route-stops-being-the-
unguarded-default/C2.md` for the full spec, and `action_guard.py`'s own
module docstring for why the ownership leg (`assert_paths_in_session_scope`)
must NOT be reached on this path with no verified caller identity.

Negative-spec:
    - Does NOT exercise the ownership leg -- `restrict_to_session` stays
      unset in every test here; C5 owns that leg.
    - Does NOT re-implement `_pathspec_element_is_sweeping` -- these tests
      drive the real predicate through `commit_paths`, not a mock of it.

Spec backlink: docs/plans/2026-08-30-the-op-route-stops-being-the-unguarded-default.md, chunk C2
"""

from __future__ import annotations

import subprocess

import pytest

# Import order matters here: `commit.py` lazy-imports `action_guard` inside
# function bodies specifically because `action_guard` -> `scope_report` ->
# `safe_commit_offer` -> `commit.py` is circular if `action_guard` loads
# before `commit.py` finishes defining `CommitRefused`/`CommitOutcome`
# (see `commit.py`'s own module docstring). Import `gcommit` first so this
# test file never exercises an import order production code doesn't use.
from coordinator_core.git import commit as gcommit
from coordinator_core.git import action_guard
from coordinator_core.git.commit import CommitDeniedByActionGuard, CommitRefused
from coordinator_core.ops.session import scope_report
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN,
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/z")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_a_sweeping_pathspec_is_denied_with_no_restrict_to_session(repo):
    """THE NEGATIVE FIXTURE. Constructs a sweeping pathspec (a magic-
    pathspec `-A` flag token, per `_pathspec_element_is_sweeping`'s own
    docstring) and calls `commit_paths` with NO `restrict_to_session` -- the
    plain default-caller shape. `-A` is not an OS directory, so it reaches
    the shape-check predicate rather than the earlier is-a-directory refusal
    -- a test that only asserted the happy path would still pass on a
    reverted change; this one fails if the default-path call to `assert_
    pathspec_shape_permitted` is removed.

    Asserts `CommitDeniedByActionGuard`, the exception `action_guard`
    raises directly for a shape deny (review: overengineering-reviewer
    Finding 7, 2026-08-30 -- collapsed from a separate `action_guard`-owned
    exception `commit_paths` used to catch and re-raise). A `CommitRefused`
    subclass, so every existing caller's
    `except (CommitRefused, FilterUnsupported)` still catches a
    pathspec-shape deny."""
    with pytest.raises(CommitDeniedByActionGuard):
        gcommit.commit_paths(repo_root=repo, paths=["-A"], message="sweep")


def test_a_sweeping_pathspec_deny_is_also_caught_by_except_commit_refused(repo):
    """PINS THE PROPERTY THE P1 FIX BOUGHT: `CommitDeniedByActionGuard` is a
    `CommitRefused` subclass, so every existing `commit_paths` caller's
    `except (CommitRefused, FilterUnsupported)` keeps catching a
    pathspec-shape deny with no edit to those five call sites. A future
    refactor that detached `CommitDeniedByActionGuard` from `CommitRefused`
    would silently un-buy this and reintroduce Finding 1 (an uncaught deny
    surfacing as an unhandled exception) -- this test reds first."""
    with pytest.raises(CommitRefused):
        gcommit.commit_paths(repo_root=repo, paths=["-A"], message="sweep")


def test_a_sweeping_pathspec_deny_surfaces_as_commit_refused_with_reason(repo):
    """FINDING 1 REGRESSION: a sweeping pathspec must surface to a caller as
    `CommitRefused` (the exception every known `commit_paths` caller already
    catches) while still carrying the guard's deny reason verbatim, proving
    the guard actually fired rather than some unrelated `CommitRefused`
    being raised first."""
    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(repo_root=repo, paths=["-A"], message="sweep")

    reason = str(excinfo.value)
    assert reason, "the raised CommitRefused must carry the guard's deny reason"


def test_an_ordinary_pathspec_still_commits_with_no_restrict_to_session(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")

    gcommit.commit_paths(repo_root=repo, paths=["a.txt"], message="ordinary")

    assert _git(repo, "log", "--format=%s").stdout.splitlines()[0] == "ordinary"


def test_default_path_never_reaches_the_ownership_leg(monkeypatch, repo):
    """ACCEPTANCE CRITERION: `assert_paths_in_session_scope` is NOT reached
    on the default path. Spies on the ownership predicate's own
    module-level binding (`coordinator_core.ops.session.scope_report.
    assert_paths_in_session_scope`) and asserts it is never called for an
    ordinary default-path commit -- proving the "ownership-leg-free" claim
    rather than merely asserting it in prose.

    Structural note: `action_guard.assert_pathspec_shape_permitted` calls
    `block_subagent_commit._pathspec_shape_permitted` directly (review:
    overengineering-reviewer Finding 8, 2026-08-30) -- a function that never
    imports or threads `assert_paths_in_session_scope` at all, so this spy
    proves the ownership leg is structurally unreachable on this path, not
    merely unexercised by this call's inputs."""
    calls = []
    monkeypatch.setattr(
        scope_report,
        "assert_paths_in_session_scope",
        lambda *a, **k: calls.append((a, k)) or (True, ""),
    )

    (repo / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
    gcommit.commit_paths(repo_root=repo, paths=["b.txt"], message="no ownership leg")

    assert calls == [], (
        "the default commit_paths() path reached assert_paths_in_session_"
        "scope -- it must consult only the sweeping/orphan/out-of-repo shape "
        "legs, never the ownership leg"
    )


def test_assert_pathspec_shape_permitted_takes_no_session_id_parameter():
    """Pins the wrapper's shape: no `session_id` parameter at all, so the
    fail-open-by-caller-supplied-identity shape the module docstring forbids
    is not merely unused here but structurally unreachable through it."""
    import inspect

    sig = inspect.signature(action_guard.assert_pathspec_shape_permitted)
    assert "session_id" not in sig.parameters


def test_zero_new_process_spawns_for_a_representative_commit(repo, monkeypatch):
    """DR-344: the predicate is a pure in-process call. Spies on
    `subprocess.run` to prove `commit_paths` spawns nothing extra via it
    for an ordinary, non-sweeping pathspec on the default path. Does not
    cover a hypothetical `subprocess.Popen` call site -- none exists on
    this path today, but this test would not catch one being added."""
    spawned = []
    real_run = subprocess.run

    def _spy_run(*a, **k):
        spawned.append(a)
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    (repo / "c.txt").write_text("c\n", encoding="utf-8", newline="\n")
    gcommit.commit_paths(repo_root=repo, paths=["c.txt"], message="zero spawn")

    assert spawned == [], (
        f"commit_paths() spawned {len(spawned)} process(es) via subprocess.run "
        "on the default path -- the shape-check predicate must be a pure "
        "in-process call"
    )


def test_default_axis_ignores_a_foreign_staged_path(repo):
    """C4, leg (a) -- DEFAULT AXIS (`prefer_deliberate_stage=False`, the
    existing spec). `validate-commit` warns when a peer's unrelated content
    is staged at commit time; with the default `prefer_deliberate_stage=
    False`, `commit_paths` never reads the whole staged index for undeclared
    paths -- invariant 1, "declared, never inferred". Stages a foreign path
    (`seed.txt`) into the index, then calls `commit_paths` naming only
    `own.txt` with no `prefer_deliberate_stage` -- the resulting commit must
    contain ONLY the declared path, and the foreign staged content must not
    be committed. This failing is what would make the default-case
    divergence real again (see the row's own docstring in `commit.py` and
    the escape table's `validate-commit` verdict).

    Cites DR-386 (a route may satisfy a guard's INTENT by a stronger
    property without literally evaluating the guard's predicate) and the
    same house pattern as `state/bug-backlog/2026-08-29-the-commit-v2-route-
    runs-none-of-the-fou-3e8811d511b7.yaml` / its spike-verdict discharge:
    gates excluded on named grounds, pinned by a test that reds if the
    property regresses.

    Spec backlink: state/dispatch-briefs/2026-08-30-the-op-route-stops-
    being-the-unguarded-default/C4.md
    """
    (repo / "own.txt").write_text("own\n", encoding="utf-8", newline="\n")
    (repo / "seed.txt").write_text("foreign staged content\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "seed.txt")

    outcome = gcommit.commit_paths(repo_root=repo, paths=["own.txt"], message="own only")

    committed = _git(repo, "show", "--name-only", "--format=", outcome.sha).stdout.split()
    assert committed == ["own.txt"], (
        "the default-path commit pulled in a path it was never told about -- "
        "invariant 1 (declared, never inferred) is broken"
    )
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "foreign staged content\n", (
        "the foreign staged content should remain untouched in the worktree; "
        "commit_paths must not have consulted or altered it"
    )


def test_residual_axis_prefer_deliberate_stage_substitutes_staged_bytes(repo):
    """C4, leg (b) -- RESIDUAL AXIS (`prefer_deliberate_stage=True`), the
    real DR-379 axis staff-eng Finding 2 names. `commit_paths` accepts
    `prefer_deliberate_stage: bool = False` (DR-379): when True, the
    settle-against-HEAD loop infers a deliberate stage from index-differs-
    from-HEAD and substitutes the staged blob with no per-path declaration.
    Its two named opt-in callers (`ops/session/safe_commit_offer.py`,
    `coordinator/bin/coordinator-safe-commit.py`) are the exact route this
    plan's Problem section cites as its own live observation: if a peer
    session stages bytes on a path that also appears in this caller's
    `paths`, the peer's staged blob is committed under this session's
    commit, by inference -- a peer's content entering a commit at commit
    time, `validate-commit`'s failure mode, live on the opt-in path.

    NAMED RESIDUAL, not fixed by this row -- narrowing `prefer_deliberate_
    stage`'s behaviour is out of this plan's proportionality and belongs to
    whichever plan owns `safe_commit_offer`'s contract. This leg only
    characterizes existing behaviour so the residual's boundary is
    checkable rather than assumed; it does not force a code change here.

    Fixture: a tracked path (`shared.txt`) whose index holds a foreign
    staged blob (`v2-staged`) differing from BOTH HEAD (`v1`) and the
    worktree (`v3-worktree`), named in this caller's own `paths` with no
    `prefer_staged` declaration -- `prefer_deliberate_stage=True` asserts
    the staged bytes win, per `commit_paths`'s own DR-379 contract.

    Spec backlink: state/dispatch-briefs/2026-08-30-the-op-route-stops-
    being-the-unguarded-default/C4.md
    """
    (repo / "shared.txt").write_text("v1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-q", "-m", "shared v1")

    (repo / "shared.txt").write_text("v2-staged\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "shared.txt")
    (repo / "shared.txt").write_text("v3-worktree\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(
        repo_root=repo,
        paths=["shared.txt"],
        message="residual axis",
        prefer_deliberate_stage=True,
    )

    assert outcome.staged_preferred == ("shared.txt",), (
        "prefer_deliberate_stage=True must move the settled candidate into "
        "staged_preferred, the same observable outcome as a declared "
        "prefer_staged path"
    )
    committed_blob = _git(
        repo, "show", f"{outcome.sha}:shared.txt"
    ).stdout
    assert committed_blob == "v2-staged\n", (
        "the caller declared prefer_deliberate_stage=True, so the staged "
        "bytes (the peer/foreign blob) must win over the worktree bytes -- "
        "this is the named residual, characterized rather than fixed here"
    )

# test_measured_process_time_for_the_default_path_call was deleted here
# (review-integrator, Finding 4): its only assertions were
# `>= 0.0` on a `time.process_time()` delta, which is tautologically true
# regardless of what commit_paths() does -- it could never fail and
# provided no regression protection. This repo measures process time and
# spawn count as a matter of discipline (DR-344), but a measurement wants
# a printed/recorded number in a benchmark, not a vacuously-passing pytest
# slot under `pytest.mark.cadence`.
