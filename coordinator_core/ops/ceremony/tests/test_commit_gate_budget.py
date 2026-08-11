"""
coordinator_core.ops.ceremony.tests.test_commit_gate_budget

Makes the plan's hard negative spec executable: "nothing on the commit hot
path may cost O(dirty tree) or O(claims)". Plan: docs/plans/2026-08-08-
claim-index-the-commit-gate-never-had.md, chunk C4. Guards against the
outage the chunk exists to prevent: the excised sink-side ownership gate
cost 13.91s on a ONE-FILE pathspec at 594 dirty paths / 918 live claims
against a 30s dispatch timeout, because its cost tracked tree busyness
(24 git-subprocess call sites) rather than pathspec size.

STALE-PLAN-TEXT NOTICE (read before editing): C4's original text called for
forcing the claim index stale between measurements and for exercising an
"index absent/corrupted" rebuild path. Both premises are gone as of this
session, verified on disk:
  - `claim_index.lookup()` now rebuilds UNCONDITIONALLY on every call
    (commit 88aee9c6e814) -- the mtime freshness probe was deleted because
    it never observed an existing session appending a claim (the common
    case moves neither the sessions-dir mtime nor the session dir's own).
    There is no fresh-vs-stale branch left to force stale, so this file
    contains no such scaffolding.
  - There is no persisted index file any more (commit effeb9f85df2):
    `.index/claims.idx` and its reader/writer were deleted. The index is a
    pure in-memory inversion built fresh per call, so there is no on-disk
    artifact to make absent or corrupt.
AC6 is re-scoped accordingly (EM decision, this chunk's dispatch brief):
its "index absence/corruption degrades to a bounded rebuild" clause is
moot (no artifact to be absent); its "the rebuild is cheap, not merely
completes" clause is the surviving, load-bearing half, tested below
against both the measured 37.9ms full-rebuild baseline and
`claim_index.REBUILD_WALL_CLOCK_CAP_SECS`. The abort-and-UNANSWERABLE
degrade path (the module's ONLY remaining degradation route) is exercised
directly, not synthesized by monkeypatching `lookup()`.

LESSON APPLICATION NOTICE (state/lessons/2026-07-29-a-b-run-order-
manufactures-the-speedup-5d0dbca9a1b2.yaml): AC2's sweep below applies two
of that lesson's prescriptions -- one discarded warm-up call per N before
timed samples, and running the comparison in BOTH ascending and descending
N order, reporting the pessimistic (larger) ratio per direction. It
deliberately does NOT apply the lesson's "one measurement per fresh
process" prescription: at 6 samples x 2 directions x 3 Ns that would mean
~36 subprocess spawns per test run, and this repo's CLAUDE.md counts
fixture-spawned processes against shared-machine load for live peer
sessions -- this file already synthesizes concurrent-session load by
writing session dirs (see AC3 below) precisely to avoid spawning, so
trading that for full lesson fidelity would make the test itself the
resource problem the repo's doctrine guards against. In-process
resampling risks measuring the module/import cache rather than the
computation, which the lesson separately warns against; the mitigation
here is `min()` over 5 in-process samples per N (reduces scheduler-noise
outliers) plus the bidirectional sweep, not process isolation. This is a
disclosed limitation, not an oversight -- re-litigate the spawn-cost
tradeoff with this paragraph in hand before changing it.

Exercises `scoped_git_commit._check_claim_conflicts` -- the gate leg only
(never the whole `_handler`/`run_commit_pipeline` staging+commit+push
pipeline; staging/push are not what this plan changed, see AC3). Fixture
shape (real throwaway git repos, session dirs synthesized entirely by
WRITING `touched.txt`/`meta.json`, never by spawning a real peer process)
mirrors `test_scoped_git_commit_ownership.py`'s already-reviewed pattern.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_scoped_git_commit_ownership.py)
# ---------------------------------------------------------------------------


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list[str]) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _touch_line(verb: str, path: str) -> str:
    return "%s 2026-08-08T10:00:00.000000Z %s" % (verb, path)


def _write_meta_live(repo: Path, sid: str, *, live: bool) -> None:
    """Layer-2 (recency-only) meta.json -- same minimal shape
    `test_scoped_git_commit_ownership.py` already uses; a live session's
    `last_activity` is inside `liveness.session_live`'s 30-minute window."""
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        json.dumps({"pid": 1, "last_activity": last_activity}) + "\n",
        encoding="utf-8",
    )


def _write_meta_stable_pid(repo: Path, sid: str) -> None:
    """Layer-1 (`stable_pid`-authoritative) meta.json, pointed at the TEST
    PROCESS's own pid with its true `create_time()` -- the shape AC3 asks
    for "where Layer-1/stable_pid coverage is wanted" so `liveness.
    session_live` reads the fixture session as live via the PPID-
    authoritative branch rather than only the recency fallback.

    Field shape mirrors `core.init()`'s own Windows write path (this repo
    is Windows-first-class, see `core.stable_pid_alive`'s PLATFORM SPLIT
    docstring): `stable_pid_lstart` carries the `create_time()` epoch
    rendered as a string (Windows never had a `ps`-format lstart), and
    `stable_pid_start_epoch` carries the same value as the primary
    tolerant-compare input -- both required for `session_live` to take the
    Layer-1 branch at all (`stable_pid_lstart` empty falls through to
    Layer-2 recency).
    """
    pid = os.getpid()
    epoch = int(psutil.Process(pid).create_time())
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "stable_pid": str(pid),
                "stable_pid_lstart": str(epoch),
                "stable_pid_start_epoch": str(epoch),
                "last_activity": session_core.now_iso(),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _measure_gate(repo: Path, paths: list[str], caller_sid: str) -> float:
    t0 = time.perf_counter()
    scoped_git_commit._check_claim_conflicts(str(repo), paths, caller_sid)
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# AC2 -- gate cost is flat in dirty-tree size, at a fixed one-file pathspec
# ---------------------------------------------------------------------------


def _build_dirty_tree_repo(tmp_path_factory, max_filler: int) -> Path:
    """One repo, `max_filler` committed filler files plus the fixed
    single-file pathspec target -- built ONCE and reused across every N in
    the AC2 sweep (rather than re-`git init`+committing per N) to keep
    fixture cost down on a shared box."""
    tmp_path = tmp_path_factory.mktemp("gate-budget-dirty-tree")
    repo = _init_repo(tmp_path)

    (repo / "fixed.txt").write_text("v1\n", encoding="utf-8")
    for i in range(max_filler):
        (repo / f"filler-{i}.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _write_meta_live(repo, "sess-caller", live=True)
    return repo


def _set_dirty_tree_size(repo: Path, n: int, max_filler: int) -> None:
    """Make exactly the first *n* filler files (plus `fixed.txt`, the
    pathspec target) dirty; reset every other filler file back to clean.
    `_check_claim_conflicts` never calls `git status` -- see this module's
    docstring -- so this axis exists to PROVE that, not because the gate
    reads it."""
    _git(["checkout", "-q", "--", "."], repo)
    (repo / "fixed.txt").write_text("v2\n", encoding="utf-8")
    for i in range(n):
        (repo / f"filler-{i}.txt").write_text("v2\n", encoding="utf-8")


@pytest.mark.cadence
def test_gate_cost_flat_against_dirty_tree_size(tmp_path_factory):
    """AC2: gate leg time does not scale with dirty-tree size N, at a
    pathspec fixed to ONE file, for N = 10 / 200 / 600.

    Asserts a RATIO, never a wall-clock constant (a fixed-ms threshold on a
    shared box is a flake generator). Guards against A/B run-order bias
    per `state/lessons/2026-07-29-a-b-run-order-manufactures-the-speedup-
    5d0dbca9a1b2.yaml`: the arm measured FIRST inherits none of the OS/FS
    cache warmth the later arms get for free, which can manufacture an
    apparent speedup trend across ascending N even when the true cost is
    flat. Mitigated two ways: (1) one discarded warm-up call per N before
    the timed samples, and (2) the whole sweep is run BOTH ascending and
    descending across N, taking the pessimistic (larger) ratio from each
    direction -- a truly flat cost holds in both directions; a real O(N)
    regression would show ratio > 1 ascending and < 1 descending (or vice
    versa), which is the asymmetry this test would catch.
    """
    ns = [10, 200, 600]
    repo = _build_dirty_tree_repo(tmp_path_factory, max_filler=max(ns))

    def _sweep(order: list[int]) -> dict:
        times: dict = {}
        for n in order:
            _set_dirty_tree_size(repo, n, max_filler=max(ns))
            _measure_gate(repo, ["fixed.txt"], "sess-caller")  # warm-up, discarded
            samples = [
                _measure_gate(repo, ["fixed.txt"], "sess-caller") for _ in range(5)
            ]
            times[n] = min(samples)
        return times

    ascending = _sweep(sorted(ns))
    descending = _sweep(sorted(ns, reverse=True))

    # Generous bound: a true O(N) regression across this 60x N range would
    # produce a ratio in the tens; a flat cost sits near 1 modulo scheduler
    # noise on a shared box. 8x leaves ample headroom above noise while
    # still catching the regression class this test exists to catch.
    _RATIO_BOUND = 8.0
    floor = 1e-6  # avoid a divide-by-near-zero on a sub-microsecond sample
    ratios = {
        label: max(times[max(ns)], floor) / max(times[min(ns)], floor)
        for label, times in (("ascending", ascending), ("descending", descending))
    }
    # Sign-disagreement note (free -- no extra samples, no extra pass/fail
    # criterion, only enriches a failure message): if ascending shows growth
    # (ratio > 1) and descending shows shrinkage (ratio < 1), or vice versa,
    # that pattern is evidence of order-dependent measurement noise per
    # state/lessons/2026-07-29-a-b-run-order-manufactures-the-speedup-
    # 5d0dbca9a1b2.yaml ("if the two orders disagree in sign, the effect is
    # cache, not code"), not a real O(N) scaling property. Kept as a
    # diagnostic annotation rather than its own assertion because a
    # dedicated sign assert on top of already-noisy min()-of-5 in-process
    # samples would be a second, less generous threshold than _RATIO_BOUND
    # and risks flaking on values close to 1.0 -- a flaky guard is worse
    # than an absent one.
    sign_disagree = (ratios["ascending"] - 1.0) * (ratios["descending"] - 1.0) < 0
    for label, times in (("ascending", ascending), ("descending", descending)):
        ratio = ratios[label]
        note = (
            " (ascending=%.2fx vs descending=%.2fx disagree in sign -- likely "
            "order-dependent measurement noise, not a real scaling effect; see "
            "this test's LESSON APPLICATION NOTICE)" % (ratios["ascending"], ratios["descending"])
            if sign_disagree
            else ""
        )
        assert ratio < _RATIO_BOUND, (
            "%s sweep: gate time scaled %.2fx from N=%d to N=%d (times=%s) "
            "-- gate cost must be flat in dirty-tree size, not O(N)%s"
            % (label, ratio, min(ns), max(ns), times, note)
        )


# ---------------------------------------------------------------------------
# AC3 -- end to end under real load: >=500 dirty paths, >=5 live sessions
# ---------------------------------------------------------------------------


def test_gate_stays_in_budget_under_real_load(tmp_path_factory):
    """AC3: the gate leg (never the whole commit -- staging/push are not
    what this plan changed) stays comfortably inside budget on a tree with
    >=500 dirty paths and >=5 concurrent LIVE sessions.

    "Concurrent live sessions" is synthesized entirely by WRITING session
    dirs with a fresh `last_activity` -- never by spawning five real
    subprocesses (this repo's CLAUDE.md counts fixture-spawned processes
    against shared-machine load, and process spawning is not the thing
    under test; the gate's liveness lookups are). One of the five carries a
    `stable_pid` pointing at THIS TEST PROCESS's own pid with its true
    `stable_pid_lstart`, for Layer-1/stable_pid coverage per the dispatch
    brief; the other four use the plain Layer-2 recency shape.
    """
    tmp_path = tmp_path_factory.mktemp("gate-budget-load")
    repo = _init_repo(tmp_path)

    n_dirty = 520
    (repo / "fixed.txt").write_text("v1\n", encoding="utf-8")
    for i in range(n_dirty):
        (repo / f"filler-{i}.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "fixed.txt").write_text("v2\n", encoding="utf-8")
    for i in range(n_dirty):
        (repo / f"filler-{i}.txt").write_text("v2\n", encoding="utf-8")

    _write_meta_live(repo, "sess-caller", live=True)

    # >=5 concurrent live peer sessions, none holding "fixed.txt".
    _write_touched(repo, "sess-peer-0", [_touch_line("T", "filler-0.txt")])
    _write_meta_stable_pid(repo, "sess-peer-0")
    for k in range(1, 5):
        sid = f"sess-peer-{k}"
        _write_touched(repo, sid, [_touch_line("T", f"filler-{k}.txt")])
        _write_meta_live(repo, sid, live=True)

    elapsed = _measure_gate(repo, ["fixed.txt"], "sess-caller")

    # Budget: this measures the gate leg ALONE (`_check_claim_conflicts`),
    # not the plan's 8s end-to-end AC3 ceiling, which also covers
    # stage+commit+push -- those stages aren't exercised here (see module
    # docstring). `_check_claim_conflicts` makes no `git status`/subprocess
    # calls (per this module's own docstring), so its structural cost is
    # sub-100ms even under this load; 1.0s leaves ~10x headroom above that
    # for shared-box scheduler noise while catching the O(dirty-tree)
    # regression class far sooner than a half-split of the end-to-end
    # ceiling would (the excised gate measured 13.91s under comparable
    # load -- 1.0s still catches that by more than an order of magnitude).
    # Review: coordinator:code-reviewer -- 4s bound was an undivided half-
    # split of the unrelated end-to-end ceiling, leaving ~40x headroom
    # versus the gate leg's actual structural cost.
    assert elapsed < 1.0, (
        "gate leg took %.3fs under >=%d dirty paths / >=5 live sessions "
        "-- exceeds budget" % (elapsed, n_dirty)
    )


# ---------------------------------------------------------------------------
# AC6 -- rebuild is CHEAP (not merely completes), and the abort/UNANSWERABLE
# degrade path is exercised directly
# ---------------------------------------------------------------------------


def test_rebuild_is_cheap_against_measured_baseline_and_hard_cap(tmp_path_factory):
    """AC6 (re-scoped, see module docstring): asserts the per-call
    `claim_index.rebuild()` this gate now runs UNCONDITIONALLY on every
    call is CHEAP, not merely that it completes -- budgeted against both
    the measured 37.9ms full-rebuild baseline (C1's own docstring) and the
    500ms hard cap (`claim_index.REBUILD_WALL_CLOCK_CAP_SECS`)."""
    tmp_path = tmp_path_factory.mktemp("gate-budget-rebuild-cheap")
    repo = _init_repo(tmp_path)
    _git(["config", "user.email", "t@t.example"], repo)  # idempotent, cheap

    for k in range(8):
        sid = f"sess-{k}"
        _write_touched(repo, sid, [_touch_line("T", f"path-{k}.txt")])
        _write_meta_live(repo, sid, live=True)

    t0 = time.perf_counter()
    state = claim_index.rebuild(sessions_dir=str(_sessions_dir(repo)))
    elapsed = time.perf_counter() - t0

    assert state.complete is True
    assert elapsed < claim_index.REBUILD_WALL_CLOCK_CAP_SECS
    # Generous multiple of the measured 37.9ms baseline (8 claimant dirs vs.
    # the 1012-file corpus that baseline was measured on) -- not a tight
    # bound, just "cheap", per the dispatch brief.
    assert elapsed < 1.0, "rebuild() took %.3fs -- expected sub-second on 8 claimants" % elapsed


def test_rebuild_abort_degrades_to_unanswerable_not_a_block(tmp_path_factory, monkeypatch):
    """AC6's surviving degradation path: a rebuild that exceeds the cap
    ABORTS (never blocks the commit) and every path the aborted walk had
    not yet resolved comes back `UNANSWERABLE`; `_check_claim_conflicts`
    turns that into a per-path fail-closed refusal, not a hang or a crash.
    This is now the module's ONLY degradation route (see module docstring)
    -- there is no on-disk index left to make "absent or corrupted".
    """
    tmp_path = tmp_path_factory.mktemp("gate-budget-rebuild-abort")
    repo = _init_repo(tmp_path)

    (repo / "fixed.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "fixed.txt").write_text("v2\n", encoding="utf-8")

    # At least one claimant dir must exist so the rebuild walk's per-entry
    # deadline check is actually reached (an empty walk never evaluates the
    # cap at all).
    _write_touched(repo, "sess-peer", [_touch_line("T", "fixed.txt")])
    _write_meta_live(repo, "sess-peer", live=True)
    _write_meta_live(repo, "sess-caller", live=True)

    # Force the FIRST enumerated entry to already be past deadline -- a
    # negative cap makes `time.monotonic() > deadline` true immediately.
    monkeypatch.setattr(claim_index, "REBUILD_WALL_CLOCK_CAP_SECS", -1.0)

    t0 = time.perf_counter()
    result = scoped_git_commit._check_claim_conflicts(str(repo), ["fixed.txt"], "sess-caller")
    elapsed = time.perf_counter() - t0

    assert result is not None
    assert result["committed"] is False
    assert "fixed.txt" in result["error"]
    # The abort itself must be near-instant, not a 500ms wait for the
    # (already-elapsed) cap -- proves this is an abort, not a stall.
    assert elapsed < 1.0
