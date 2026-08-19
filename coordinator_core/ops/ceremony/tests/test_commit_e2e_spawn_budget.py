"""
coordinator_core.ops.ceremony.tests.test_commit_e2e_spawn_budget

C8 (docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-clear.md),
A8/A10: gives `ceremony.scoped_git_commit` an END-TO-END git-subprocess
SPAWN-COUNT budget -- the whole `_handler`/`run_commit_pipeline`
staging+commit+push pipeline, not just the ownership-gate leg.

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent
LLM sessions at any given moment (CLAUDE.md's "Load norm" section) -- a
wall-clock assertion on this op would be noise, not signal. The spawn
count this module asserts is invariant to machine load; it only moves when
the CODE changes how many git subprocesses it issues per invocation shape,
which is exactly the regression class this budget exists to catch (an
accidental +5 landing unmeasured, the way C5's own +1/+2 nearly did).

COUNTING MECHANISM: wraps `git_native._git`, the choke point for the
staging and commit calls the gate leg issues, rather than inventing a
second counting dialect. (This approach was inherited from a
`test_commit_gate_budget.py` that no longer exists in the tree; the
mechanism outlived the module it came from.) It does
not itself spawn per-path -- it counts real calls the pipeline already
makes, it never issues extra ones.

THE BLIND SPOT, AND WHAT CLOSED MOST OF IT (opro-03 C6, 2026-08-19).
`git_native._git` is NOT the sole spawn mechanism reachable from
`_handler`. The push-drain leg (`run_commit_pipeline` ->
`commit_pipeline._drain_pending_push_after_sync` ->
`auto_push.drain_pending_push`), claim release
(`session/scope.py :: _git_run`), `git/divergence.py :: _run_git`, and
`git/repo_root.py :: _spawn_rev_parse` all spawn git without going
through `_git()`, so wrapping `_git()` cannot see them -- the seam is
mechanism-robust but ROUTING-NARROW, by construction.

The fix was a second counter, not a reroute. `_count_op_spawns_both_ways`
also substitutes the `subprocess.run` module attribute, scoped to the op
invocation, and the `op_total_*` manifest keys pin the result: 23/24/28
against the seam's 17/18/20. Rerouting those legs through `_git()` was
the alternative and was rejected -- `auto_push` is a hook with callers
outside this pipeline, so it would have changed their behaviour to buy a
measurement.

Three of the seven bypasses were counted this way first (2026-08-19, first
pass). The remaining three -- `push_once`, `_is_ancestor` and
`_invoke_cockpit_publish` -- sat on the non-fast-forward/superseded branch
of `run_push_with_retry`, which no fixture here took (the op's own commit
pushes cleanly; the pending-push drain never fired on a divergent branch;
no fixture repo carried a cockpit publish script).
`test_pending_drain_superseded_path_spawn_count_matches_budget` (below,
opro-03 C6 second pass) closes all three the same way: a due pending-push
record naming a SECOND, synthetic branch the op never itself commits on,
engineered non-fast-forward-superseded against origin, with a planted
Cockpit-publish script and a schema-touching commit -- counted by
`op_total_pending_drain_superseded` (39) against that shape's own seam
figure (23). `repo_root :: _spawn_rev_parse` never spawns at all because
`show_toplevel` walks for any tree that has a `.git`, and stays uncounted
by construction, not by fixture gap. All four are enumerated with their
evidence in
`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py`.

MEASURED-SHAPES CAVEAT: the `green_path: 17` figure and its siblings are
not miscounts -- this fixture builds a repo with no remote and no
upstream, so `_resolve_push_report` short-circuits; the numbers are
correct for the shapes this fixture measures. Treating that as
completeness is what was wrong, and `op_total_green_path: 23` is the part
that now moves when a bypass grows.

MEASURED SHAPES (re-baselined 2026-08-18, this repo, matching
`budget-manifest.json`'s `overrides["ceremony.scoped_git_commit"].
spawn_count_budget`):
  - green path (clean commit, no claim conflict): 17 spawns.
  - refusal path (an UNANSWERABLE path -- unverified caller identity
    conflicting with a live claimant): 0 spawns -- `_handler` returns the
    rejection before any staging call is ever issued.
  - a directory-pathspec-expansion invocation: 18 spawns -- the green
    path's 17 plus the one extra pathspec-scoped `git status --porcelain
    -- <dir>` `_expand_directory_pathspecs` issues up front.
  - the push-raced path: 20 spawns and ZERO hot-path `time.sleep`. Was 24
    and 1.0s while `_remote_sha_state` lived on it (opro-01 C-09 made it
    countable, C-01/C-02 removed it).
  - the pending-drain-superseded path (opro-03 C6, 2026-08-19): 23 spawns
    on the `git_native._git` seam (this fixture's own green-path-shaped
    commit on `work/main`, plus its extra branch/bookkeeping calls) and 39
    on the whole-op `subprocess.run` counter -- the +16 is `push_once`,
    `_is_ancestor`, and `_invoke_cockpit_publish` finally firing, along
    with the rest of `auto_push`'s drain/retry bookkeeping.

The first three were 12/0/13 when measured 2026-08-11. The +5 on the two
non-zero shapes is the agree-branch CAS's doubled index/HEAD blob read
(`_index_blobs`/`_head_blobs` at capture, re-read in
`_agree_branch_cas_refusal` before the agree branch's `git add`) plus
`interpret-trailers --no-divider --in-place` -- all of it landed between
that date and 2026-08-18 without moving the budget. This module carries
`pytest.mark.cadence`, so the fast tier never ran the assertion that
would have caught it the day it drifted; that, not the count, is the
part worth remembering.

These are exact-equality assertions, deliberately: the whole point of a
spawn-COUNT budget is that it does not drift quietly. A future op change
that adds or removes a spawn must update both this test and the manifest
entry in the same commit, not silently pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_meta_live(repo: Path, sid: str, *, live: bool) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        json.dumps({"pid": 1, "last_activity": last_activity}) + "\n",
        encoding="utf-8",
    )


def _count_op_spawns_both_ways(fn) -> tuple[int, int, dict]:
    """Run *fn* ONCE under both counters and return `(native_git_n, all_n, result)`.

    One invocation, two figures, on purpose: this op stages and commits, so it is
    not idempotent and cannot be run twice to collect a second measurement.

    `all_n` substitutes the `subprocess.run` MODULE ATTRIBUTE, making it
    routing-agnostic where the `git_native._git` seam is routing-narrow — a spawn
    added anywhere in the op's reached set is counted even when it never touches
    that seam. That is exactly what the seven `ceremony.scoped_git_commit`
    bypasses exploit (`auto_push`, `session/scope.py`, `git/divergence.py` all
    call `subprocess.run` directly), and it is the property an `op_total_*` key
    needs and a seam-counted key structurally cannot provide.

    SCOPED TO `fn` ON PURPOSE. A counter installed for the whole test would also
    catch the fixture's own repo-building git calls — measured 2026-08-19 at
    28/29/37 per test, most of it fixture — so a test-scoped figure would move
    whenever the fixture changed. Same defect class as a figure measured against
    a stubbed op.

    `all_n`'s own hole is mechanism, not routing: it sees `subprocess.run` and not
    `Popen`/`os.posix_spawn`/`asyncio.create_subprocess_exec`. That precondition
    is enforced per-site by `coordinator_core/tests/
    test_no_uncounted_spawn_on_budgeted_path.py::
    test_legitimized_site_mechanism_pins_hold`; see that module's operational
    definition of "counted" for why the two counter shapes are complementary
    rather than ranked."""
    seam = {"n": 0}
    allc = {"n": 0}
    orig_git = git_native._git
    orig_run = subprocess.run

    def _git_wrapper(*a, **kw):
        seam["n"] += 1
        return orig_git(*a, **kw)

    def _run_wrapper(*a, **kw):
        allc["n"] += 1
        return orig_run(*a, **kw)

    git_native._git = _git_wrapper
    subprocess.run = _run_wrapper
    try:
        result = fn()
    finally:
        git_native._git = orig_git
        subprocess.run = orig_run
    return seam["n"], allc["n"], result


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["ceremony.scoped_git_commit"]
    return entry["spawn_count_budget"]


def _assert_op_total(shape: str, n_all: int) -> None:
    """Pin the WHOLE-OP spawn count for *shape* against its `op_total_<shape>`
    manifest key (opro-03 C6).

    Separate key rather than a raised `<shape>` figure, deliberately: the two
    measure different things and must not be conflated. `<shape>` is the
    `git_native._git` seam — the op's own native-git calls — and stays the right
    number for reasoning about that seam. `op_total_<shape>` is every process the
    op spawns by any route, which is the only figure the seven bypasses can move.
    Collapsing them would delete the seam figure the pipeline is actually
    designed around; raising the seam figure to cover spawns it cannot observe
    would be a bound fitted to an unobserved set, which this plan's anti-scope
    forbids outright.

    The gap between them is the point, not an error to tune away: measured
    2026-08-19 at +6/+6/+8, and it is exactly the uncounted-bypass surface
    `test_no_uncounted_spawn_on_budgeted_path.py` enumerates. Shrinking that gap
    by routing a bypass through the counted seam is progress; shrinking it by
    editing this number is the failure."""
    budgeted = _manifest_spawn_budget()[f"op_total_{shape}"]
    assert n_all == budgeted, (
        "%s WHOLE-OP spawn count is %d, manifest budgets %d. This figure counts "
        "every process the op spawns, not just the `git_native._git` seam, so it "
        "moves when a bypass is added ANYWHERE in the op's reached set -- which is "
        "what it is for. Update budget-manifest.json's ceremony.scoped_git_commit."
        "spawn_count_budget.op_total_%s (and its _rationale) in the same commit if "
        "the change is intentional; never edit it to make a new bypass fit."
        % (shape, n_all, budgeted, shape)
    )


# ---------------------------------------------------------------------------
# A10: three invocation shapes, pinned against budget-manifest.json
# ---------------------------------------------------------------------------


def test_green_path_spawn_count_matches_budget(tmp_path_factory):
    """A10, green path: a clean, unclaimed commit costs the manifest's
    `spawn_count_budget.green_path` git subprocesses, end to end."""
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-green")
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["a.txt"],
                "message": "test commit",
            }
        )

    n, n_all, result = _count_op_spawns_both_ways(_run)
    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)

    _assert_op_total("green_path", n_all)

    budgeted = _manifest_spawn_budget()["green_path"]
    assert n == budgeted, (
        "green-path e2e spawn count is %d, manifest budgets %d -- update "
        "budget-manifest.json's ceremony.scoped_git_commit.spawn_count_"
        "budget.green_path (and its _rationale) together with this test "
        "if the change is intentional" % (n, budgeted)
    )


def test_directory_pathspec_expansion_spawn_count_matches_budget(tmp_path_factory):
    """A10, directory-pathspec-expansion: a directory element with dirty
    tracked content beneath it (`_expand_directory_pathspecs`) costs the
    manifest's `spawn_count_budget.directory_pathspec_expansion` git
    subprocesses, end to end -- green_path's cost plus the one extra
    pathspec-scoped expansion probe."""
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-dir-expand")
    repo = _init_repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("v1\n", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "sub" / "a.txt").write_text("v2\n", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["sub"],
                "message": "test commit",
            }
        )

    n, n_all, result = _count_op_spawns_both_ways(_run)
    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)

    _assert_op_total("directory_pathspec_expansion", n_all)

    budgeted = _manifest_spawn_budget()["directory_pathspec_expansion"]
    assert n == budgeted, (
        "directory-pathspec-expansion e2e spawn count is %d, manifest "
        "budgets %d -- update budget-manifest.json's ceremony."
        "scoped_git_commit.spawn_count_budget.directory_pathspec_expansion "
        "(and its _rationale) together with this test if the change is "
        "intentional" % (n, budgeted)
    )


# ---------------------------------------------------------------------------
# opro-01 C-09: the probe-bearing (push-raced / push-failed) path
# ---------------------------------------------------------------------------


def _init_repo_with_upstream(tmp_path: Path) -> Path:
    """A repo on a `work/*` branch with a real upstream, whose remote URL is
    then pointed at nothing.

    Every clause is load-bearing for reaching the probe:
      - `work/*` — `coordinator-auto-push` doctrine declines to push anything
        else, and `PUSH_STATUS_DECLINED` short-circuits `_resolve_push_report`
        before `_remote_sha_state`.
      - a real first push — leaves `origin/<branch>` as a resolvable local ref,
        so `merge-base --is-ancestor` can answer 1 (definitively absent) rather
        than 128 (unknown), which is what drives the full retry loop.
      - a broken remote URL afterwards — makes this op's own push fail without
        a fast-forward reject, so `_rebase_onto_fetched_ref` stays out of the
        count.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "-q"], bare)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "work/budget/probe"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/budget/probe"], repo)
    _git(["remote", "set-url", "origin", str(tmp_path / "nonexistent.git")], repo)
    return repo


def test_push_raced_path_spawn_count_and_sleep_match_budget(tmp_path_factory):
    """C-05 after-figure: the push-raced path costs the manifest's
    `spawn_count_budget.push_raced_path` git subprocesses and sleeps ZERO.

    This is the path `_remote_sha_state` used to be reachable on -- the green
    path's fixture has no remote and no upstream, so the probe was never
    reachable there and `green_path` is unaffected by its removal. C-09 first
    routed the probe's four spawns through `git_native._git` so this counter
    could see them at all (they were bare `subprocess.run`); C-01/C-02 then
    removed the probe, taking those four spawns and the retry loop's 1.0s with
    them.

    The zero-sleep assertion is the load-bearing half. A spawn count cannot
    express a `time.sleep`, and a reintroduced retry loop would cost 1.0s per
    raced commit under a 50-70-concurrent-session load norm while showing up
    as only a small count change -- or, if it slept without spawning, as none
    at all.
    """
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-probe")
    repo = _init_repo_with_upstream(tmp_path)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    # Patched on the `time` module itself, not on any one module's imported
    # reference: C-02 removed `scoped_git_commit`'s `import time` entirely, and
    # a per-module patch would only ever catch a sleep reintroduced in the one
    # module it was aimed at. This catches one anywhere on the path.
    slept: list[float] = []
    orig_sleep = time.sleep

    def _record_sleep(secs):
        slept.append(secs)
        return orig_sleep(secs)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["a.txt"],
                "message": "test commit",
            }
        )

    time.sleep = _record_sleep
    try:
        n, n_all, result = _count_op_spawns_both_ways(_run)
    finally:
        time.sleep = orig_sleep

    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)
    assert result["push_state"] == "push-failed", (
        "fixture did not reach the probe -- push_state is %r, so "
        "_resolve_push_report short-circuited before _remote_sha_state and "
        "this test is measuring the wrong path" % (result.get("push_state"),)
    )

    _assert_op_total("push_raced_path", n_all)

    budgeted = _manifest_spawn_budget()["push_raced_path"]
    assert n == budgeted, (
        "push-raced-path e2e spawn count is %d, manifest budgets %d -- "
        "update budget-manifest.json's ceremony.scoped_git_commit.spawn_"
        "count_budget.push_raced_path (and its _rationale) together with "
        "this test if the change is intentional" % (n, budgeted)
    )
    assert slept == [], (
        "the push-raced path slept %.2fs on the hot path, expected none -- "
        "opro-01 C-02 removed the only sleep this path had "
        "(`_remote_sha_state`'s retry loop). %r" % (sum(slept), slept)
    )


# ---------------------------------------------------------------------------
# opro-03 C6: the pending-drain superseded path -- the one fixture that
# reaches all three previously-conditional `auto_push` bypasses (`push_once`,
# `_is_ancestor`, `_invoke_cockpit_publish`) off a single `_handler` call.
# ---------------------------------------------------------------------------


def _init_repo_pending_drain_superseded(tmp_path: Path) -> tuple[Path, str]:
    """A repo on `work/main` with a clean upstream (the op's own commit pushes cleanly), PLUS a
    second local branch `work/pending` deliberately left non-fast-forward-superseded against
    origin, with a cockpit-contract publish script on disk and a schema-touching commit in
    `work/pending`'s own history. Returns `(work_repo, schema_commit_sha)`.

    Every clause is load-bearing:
      - TWO branches, not one. `work/main` is the branch `_handler` actually commits on and
        must push CLEANLY (no divergence), so `push_status == PUSH_STATUS_PUSHED` and
        `_drain_pending_push_after_sync` fires at all. `work/pending` is never touched by the
        op -- its divergence is entirely synthetic, engineered below, and reached only through
        the pending record naming it. Coupling the two (making `_handler`'s own commit branch
        the superseded one) does not work: this op's OWN `push_with_retry` fetch+rebases a
        rejected push and re-pushes successfully (`commit_pipeline.py`'s own
        `_rebase_onto_fetched_ref`), resolving the divergence before `auto_push` ever sees it.
      - The cockpit-contract publish script lives on `work/main`'s own working tree (the branch
        checked out when both the op and the drain run) -- `_cockpit_publish_script` is a
        `Path.is_file()` stat against `repo_root`, independent of which branch's git history
        carries the file.
      - `work/pending`'s schema-touching commit is pushed DIRECTLY TO THE BARE REMOTE'S PATH,
        never through the `origin` remote name, so this repo's own
        `refs/remotes/origin/work/pending` is never created here. `_maybe_publish_cockpit_
        contract` then falls back to `_schema_touched`'s documented empty-tree base ("first
        push on a new branch"), so the schema commit is visible in that diff without a prior
        remote-tracking ref.
      - A SEPARATE scratch clone then adds one further commit on top of that same schema
        commit and pushes it, leaving the bare remote's `work/pending` two commits ahead of
        this repo's own `work/pending` ref (still sitting at the schema commit alone). This
        repo's own `git push origin work/pending` is then rejected non-fast-forward, and the
        schema commit (`local_sha`) IS an ancestor of the refreshed
        `refs/remotes/origin/work/pending` -- exactly `_is_superseded`'s early-return shape.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "-q"], bare)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "work/main"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / ".github" / "scripts").mkdir(parents=True)
    (repo / ".github" / "scripts" / "publish_cockpit_contract.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8",
    )
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/main"], repo)

    _git(["checkout", "-q", "-b", "work/pending"], repo)
    schema_dir = repo / "coordinator" / "cockpit-contract" / "schema"
    schema_dir.mkdir(parents=True)
    (schema_dir / "shape.json").write_text("{}\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "schema touch"], repo)
    schema_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "work/pending"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    # Direct-to-URL push -- deliberately NOT through the `origin` remote name, so this repo's
    # own `refs/remotes/origin/work/pending` is never created (see docstring above).
    _git(["push", "-q", str(bare), "work/pending:work/pending"], repo)
    _git(["checkout", "-q", "work/main"], repo)

    scratch = tmp_path / "scratch"
    _git(["clone", "-q", str(bare), str(scratch)], tmp_path)
    _git(["checkout", "-q", "work/pending"], scratch)
    (scratch / "b.txt").write_text("scratch\n", encoding="utf-8")
    _git(["add", "-A"], scratch)
    _git(["commit", "-q", "-m", "scratch extra"], scratch)
    _git(["push", "-q", "origin", "work/pending"], scratch)

    return repo, schema_sha


def test_pending_drain_superseded_path_spawn_count_matches_budget(tmp_path_factory, monkeypatch):
    """opro-03 C6: the pending-push drain's own superseded non-fast-forward branch, which
    reaches all three previously-conditional `auto_push` sites -- `push_once` (site 1, the
    retry loop's unconditional attempt), `_is_ancestor` (site 2, via `_is_superseded`), and
    `_invoke_cockpit_publish` (site 3, the early-return's own success leg) -- off one `_handler`
    call. See `coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py`'s
    `_LEGITIMIZED_SITES` entries for these three sites, and `_init_repo_pending_drain_
    superseded`'s own docstring for why the fixture needs two branches.

    The `executed` leg of each site's legitimation is measured HERE, not inferred from the
    spawn count alone -- `is_ancestor_calls`/`invoke_publish_calls` wrap the real functions via
    `monkeypatch.setattr`, so a fixture that silently short-circuits the superseded branch (and
    still happens to match the manifest's spawn count by coincidence) fails loudly instead of
    passing quietly.
    """
    tmp_path = tmp_path_factory.mktemp("e2e-spawn-pending-drain")
    repo, schema_sha = _init_repo_pending_drain_superseded(tmp_path)
    (repo / "a.txt").write_text("v2\n", encoding="utf-8")
    _write_meta_live(repo, "sess-caller", live=True)

    from coordinator_core.hooks import auto_push

    monkeypatch.setenv("COORDINATOR_AUTO_PUSH_NO_SLEEP", "1")

    wrote = auto_push._write_pending_record(
        str(repo), "work/pending", schema_sha, time.time() - 100, os.getpid()
    )
    assert wrote, "fixture could not plant the pending-push record"

    is_ancestor_calls = {"n": 0}
    invoke_publish_calls = {"n": 0}
    orig_is_ancestor = auto_push._is_ancestor
    orig_invoke_publish = auto_push._invoke_cockpit_publish

    def _is_ancestor_wrapper(*a, **kw):
        is_ancestor_calls["n"] += 1
        return orig_is_ancestor(*a, **kw)

    def _invoke_publish_wrapper(*a, **kw):
        invoke_publish_calls["n"] += 1
        return orig_invoke_publish(*a, **kw)

    monkeypatch.setattr(auto_push, "_is_ancestor", _is_ancestor_wrapper)
    monkeypatch.setattr(auto_push, "_invoke_cockpit_publish", _invoke_publish_wrapper)

    def _run():
        return scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["a.txt"],
                "message": "test commit",
            }
        )

    n, n_all, result = _count_op_spawns_both_ways(_run)

    assert result["committed"] is True, "fixture did not land a commit: %r" % (result,)
    assert result["push_state"] == "pushed", (
        "fixture's own commit did not push cleanly on work/main -- push_state is %r, so "
        "_drain_pending_push_after_sync never fires and this test is measuring the wrong path"
        % (result.get("push_state"),)
    )
    assert is_ancestor_calls["n"] >= 1, (
        "fixture did not reach auto_push._is_ancestor -- the superseded non-fast-forward branch "
        "was not taken, so this is not the path this test claims to measure"
    )
    assert invoke_publish_calls["n"] >= 1, (
        "fixture did not reach auto_push._invoke_cockpit_publish -- the cockpit-contract publish "
        "leg was not taken, so this is not the path this test claims to measure"
    )

    _assert_op_total("pending_drain_superseded", n_all)

    budgeted = _manifest_spawn_budget()["pending_drain_superseded"]
    assert n == budgeted, (
        "pending-drain-superseded e2e spawn count is %d, manifest budgets %d -- update "
        "budget-manifest.json's ceremony.scoped_git_commit.spawn_count_budget."
        "pending_drain_superseded (and its _rationale) together with this test if the change "
        "is intentional" % (n, budgeted)
    )
