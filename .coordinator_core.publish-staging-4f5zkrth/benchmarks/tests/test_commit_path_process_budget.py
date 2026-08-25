"""Standing gate: the commit path's process-time and spawn-count cost.

C3 (docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-processes.md):
"pin the commit path's process time and spawn count as a regression gate."

WHY THIS FILE EXISTS. The commit hot path reached ~1461ms/~35.6 processes one
reasonable-looking hook at a time, with nothing asserting the property (see
that plan's § Problem). C1/C2/C4 cut ~480ms of hook cost; this file is the
regression lock that keeps it cut. Follows `test_warm_door_process_time_gate
.py`'s shape (module docstring explaining the threshold's derivation, an
isolated harness, process time via the job-object primitive, never wall
clock) rather than inventing a second convention, per that file's own
instruction in the plan body.

UNIT: process time (job-object `TotalUserTime + TotalKernelTime`) and spawn
count (`TotalProcesses`), both via `batched_process_time_ms`
(k=K_INVOCATIONS), never wall clock -- CLAUDE.md § The brightline: "Process
time and spawn count, never wall clock -- wall clock measures peer load."

STAGED SHAPE (§ Resolved's EM ruling, ratified, not reopened here): a
REALISTIC staged working tree, not a one-file fixture. This file's fixture is
`N_TRACKED_FILES` (150) tracked files, each carrying `N_HISTORY_REVISIONS`
(3) prior commits touching all of them (so `detect-staged-rollback`'s history
walk has real per-path depth to traverse, not an empty log), on a `work/*`
branch (§ Anti-scope: "re-measure on the same branch kind -- a non-`work/*`
branch short-circuits auto-push and reads ~600ms cheaper for the wrong
reason"). 150 rather than this repo's own live-tree figure of ~1000 (§ The
budget the gate cannot meet's table) is a bounded-CI-runtime concession,
stated rather than hidden: the gate's cost SCALES with staged breadth and
history depth (that section's own table), so this fixture is representative
of the shape, not a literal reproduction of the live-tree number -- a reader
extrapolating this pin to the ~1000-file case should expect a materially
larger number, not this one.

TWO NAMED EXCLUSIONS, both REPORTED alongside the passing number, never in
place of it (AC1's own requirement):

  1. GATE HISTORY WALK (AC1) -- `docs/research/spike-verdicts/2026-08-21-
     staged-rollback-verdict-within-budget.md` (C5, `7984bb0119fb`,
     not-viable): no cheaper query shape reaches budget without narrowing
     detection, and detection stays whole (§ The budget the gate cannot
     meet). This file measures the gate's OWN process time in isolation
     (`_measure_gate_alone`, same read-only trampoline C4's own pin uses,
     against an equivalently-staged snapshot) and subtracts it from the
     full-commit total to produce the AC1-comparable number; the raw,
     un-excluded total is asserted and reported too, so the exclusion is
     never a way to make a red number look green.

  2. DETACHED AUTO-PUSH SUBTREE (C2's remit, this file's design call per the
     C3 task body: "Whichever C3 picks, state it in AC1 and AC5"). C2's own
     disposition recommends mechanism (b) -- "the harness-side direct-child
     measurement... NOT CREATE_BREAKAWAY_FROM_JOB" -- over (a), because
     breakaway also escapes the job's TERMINATION semantics (a killed
     session's detached push becomes an orphan; see C2's disposition_detail
     and state/audits/2026-08-21-detached-process-console-window-storm.md).
     But mechanism (b) as literally stated (GetProcessTimes on the direct
     child plus synchronously-waited descendants, job-object query left out
     entirely) is not implementable from THIS file's writable scope without
     either (a) or editing `auto_push.py` (out of C3's scope: `writes:` names
     only this manifest and this test file) to add a test-only stub seam.

     CONCRETE MECHANISM CHOSEN HERE, stated so a reader never has to guess
     it: `COORDINATOR_AUTO_PUSH_SYNC=1`. Forcing the hook down its
     synchronous branch means `_detach_and_run` (and therefore any detached
     child, and therefore the job-cascade problem breakaway would otherwise
     be needed for) never runs at all -- `main()` calls `run_push_with_retry`
     in-process instead (see that function's own body, the
     `if async_mode: _detach_and_run(...) else: run_push_with_retry(...)`
     branch). This sidesteps the job-object cascade race entirely (nothing
     to accidentally include or exclude by timing a query against a still-
     running background process) without touching `_windows_detached_flags`
     or requesting `CREATE_BREAKAWAY_FROM_JOB` -- C2's rejected mechanism (a)
     stays untouched. The honest cost of this substitution: the fixture's
     `origin` remote is a local bare repo (no network round trip), so the
     synchronous push itself is fast and bounded, but it is still real git
     spawn(s) INCLUDED in the pinned number, not silently dropped -- a
     materially different (typically cheaper, since no network wait) shape
     than production's real detached-async push, and disclosed as such here
     rather than presented as identical to it. Revisit this mechanism if a
     future chunk adds an explicit test-only detach-stub seam to
     `auto_push.py` -- that would let AC1/AC5's number drop the local-push
     cost entirely, which this file's number does not.

DRIVER RESIDUE (AC5): the measured `cmd` is a small Python wrapper
(`_write_driver`) that mutates `N_MODIFIED_PER_COMMIT` files fresh each
invocation (batched_process_time_ms re-runs the SAME argv k times, so
per-invocation staged content must come from the driver itself, not from
pre-staged state the first invocation would consume -- same trap C1's own
harness names for its idempotent-hook fixture) then runs `git add -A` and
`git commit`. The wrapper's own interpreter start + `git add -A` spawn is
driver residue, calibrated separately (`_measure_driver_residue`, same file
mutation, no commit) and subtracted from both axes -- mirroring how the
plan's own § Problem table subtracts "a 175.8ms/4-proc driver+commit
baseline" rather than assuming a fixed constant.

REMAINING SKIP CASE: off Windows (job-object accounting has no POSIX
equivalent, matching every sibling gate in this package).

WHAT THIS FILE PINS, AND WHAT IT DOES NOT (2026-08-21, on measurement, at
pickup). The first authored revision of this file carried single ceilings of
900ms / 30 processes described as "measured this session". They were not:
the first end-to-end run of this fixture produced

    raw full-commit total   1646.484ms / 40.625 procs   (k=8)
    driver residue          244.141ms  /  3.0   procs   (excluded, both axes)
    gate history walk       224.609ms  /  5.0   procs   (AC1 time exclusion)
    AC1-comparable          1177.734ms
    AC2-comparable          37.625 procs

so the file's own pin failed on its own fixture on first execution. Both
numbers are also far outside the plan's ACs (AC1 <500ms, AC2 <=12 procs),
which is the fact worth carrying: after C1/C2/C4, the commit path STILL does
not meet its budget on a realistic staged tree. A single ceiling cannot say
both "do not get worse" and "this is the budget", so this file states them
separately -- a RATCHET that is green today and catches regression, and the
AC TARGET, red by design (`designed_red`: "failure output is a worklist"),
whose failure text is the standing worklist for the remaining gap.

MEASUREMENT CAVEAT, stated rather than buried: the numbers above were taken
with `COORDINATOR_ENGINE_ROOT` pointed at the published engine mirror,
because at that moment project-makima's own working tree carried another
session's in-flight edit to `coordinator_core/ops/detect_staged_rollback.py`
(a `NameError` from a partially-applied cut). Engine content there was
verified equivalent to `HEAD` (publish-time identifier renames only).

WHY THE RATCHET IS NOT YET THE FINAL PIN. The exact-blob rollback check --
the term AC1 names as its exclusion, and the term C5 priced at 1184ms and
returned `not-viable` on -- is being DELETED under a PM ruling by C16 of
`docs/plans/2026-08-21-the-cli-bootstrap-tax-dies-at-the-interpreter-floor
.md` (check 2, the mass-deletion tripwire, is NOT cut). When that lands, the
excluded term this file subtracts largely ceases to exist and every number
above moves. Re-measure and lower the ratchet then; do not treat today's
figure as the budget C3 was asked to pin.

Spec backlink: docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-
processes.md, C3. AC1/AC2/AC5/AC6 (C5's viability verdict) are what this
file exists to satisfy.

DARWIN LEG (C7, docs/plans/2026-08-22-the-brightlines-instrument-exists-on-
the-fleet-floor.md). Everything above this note is C3's own work, scoped to
`docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-processes.md` --
its AC1 stays recorded NOT MET on Windows (`designed_red`), and nothing
below touches that bookkeeping, that plan's thresholds, or its
`designed_red` marker (C7's own AC21). The macOS leg is a SEPARATELY
IDENTIFIABLE, differently-named set of tests (`..._darwin_...`) with its own
MEASURED baseline, not a parametrisation of the Windows tests above and not
a contribution to the Windows plan's AC1/AC2 numbers -- a macOS figure must
never be readable as movement on that plan's own recorded verdict, in
either direction.

MACOS BASELINE (measured, 2026-08-22, this box, `sysctl -n hw.ncpu`-class
Apple Silicon Pro-tier laptop, same fixture as the Windows leg above:
150 tracked files / 3 history revisions / 100 modified per commit,
`COORDINATOR_AUTO_PUSH_SYNC=1`):

    driver residue          ~55-65ms    / 2.0   procs   (k=5)
    gate history-walk       ~45-50ms    / 2.0   procs   (k=5, AC1-analog time exclusion only)
    full-commit quantiles   n=3, k=5-8  p50 ~217-310ms, p90 ~198-350ms
    full-commit (k=8)       ~316-350ms  / 25.1-25.4 procs
    AC1-analog (p50/p90, residue+gate excluded)   ~191-241ms
    AC2-analog (spawn count, residue excluded)    ~23.1-23.4 procs

Three runs of this campaign (one during initial authorship, two during
in-session re-verification) landed within a wider spread than the first two
alone suggested -- consistent with this box's normal 50-70 concurrent-session
load (CLAUDE.md § Load norm). The constants below are MEASURED (not
derived) and pinned with headroom banded off the HIGHEST observed run,
labelled as such per AC20. The finding worth carrying: macOS's AC1-analog
(198-241ms) sits comfortably UNDER the plan's own 500ms target -- no
conhost, no job-object overhead, a materially cheaper process-creation
shape than Windows on the SAME fixture -- while its AC2-analog (~23.1-23.4
procs) is still well OVER the plan's 12-process target. This is a REPORTED
FINDING (spawn count, not process time, is macOS's real gap), not a
threshold this file adjusts and not grounds to touch the Windows plan.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
    batched_process_time_quantiles,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 8
"""Matches this plan's own body ("k>=8") and C4/C5's measurement discipline."""

N_TRACKED_FILES = 150
"""Bounded-CI-runtime stand-in for this repo's own ~1000-file live tree --
see module docstring's STAGED SHAPE section for why 150, not 1000."""

N_HISTORY_REVISIONS = 3
"""Prior commits touching every tracked file, so the gate's history walk has
real per-path depth (not an empty `git log`) -- matches this repo's own
measured shape in § The budget the gate cannot meet (three-revision fixture
files in that spike's own methodology)."""

N_MODIFIED_PER_COMMIT = 100
"""Staged breadth per measured commit -- realistic (not one file), bounded
for CI wall-clock. Content is always NEW relative to every historical blob
for that path, so the fixture never trips the rollback detector, and never
deletes a file, so it never trips the mass-deletion tripwire -- this pin
measures the ordinary (clean-verdict) commit path, not a blocked one."""

_BIN_DIR = Path(__file__).resolve().parents[2].parent / "coordinator" / "bin"
_DETECT_ROLLBACK = _BIN_DIR / "detect-staged-rollback.py"
_PREPARE_COMMIT_MSG = _BIN_DIR / "coordinator-prepare-commit-msg.py"
_AUTO_PUSH = _BIN_DIR / "coordinator-auto-push.py"

_MAKIMA_ROOT = str(Path(__file__).resolve().parents[2].parent)

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip("process-time job-object accounting is a Windows-only primitive")


def _require_darwin() -> None:
    if not IS_DARWIN:
        pytest.skip("process-time kqueue/EVFILT_PROC accounting is a Darwin-only primitive")


def _fwd(p) -> str:
    return str(p).replace("\\", "/")


def _git(repo, *args, check=True, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        env=env,
        **_NO_WINDOW,
    )


def _env(**overrides) -> dict:
    """Base env for every spawned child in this file -- always carrying an
    explicit `COORDINATOR_ENGINE_ROOT` (Rung 1 of `cc_invoke`'s resolution
    ladder), required because this suite's `coordinator_core/conftest.py`
    autouse fixture quarantines HOME/USERPROFILE, so nothing under a
    spawned child's real machine-local registry or `.makima-root` pointer
    is reachable -- same shape as `test_detect_staged_rollback_spawn_
    budget.py::_env`.
    """
    base = dict(os.environ)
    base.setdefault("COORDINATOR_ENGINE_ROOT", _MAKIMA_ROOT)
    base.update(overrides)
    return base


def _install_hook(repo: Path, hook_name: str, script: Path, forward_args: bool) -> None:
    """Write a minimal, bash-free `.git/hooks/<hook_name>` that `exec`s
    `script` by ABSOLUTE path -- the same shape `git_hook_install.py`'s own
    shim exec-line uses (`exec "$_PY" "$SCRIPT" "$@"`), simplified to a fixed
    interpreter (`sys.executable`) rather than that module's python3/python/py
    probe chain, since this fixture controls its own interpreter and does not
    need to rediscover it. Absolute-path invocation is load-bearing (C4's own
    § Corrections 5 finding): a relative baked path only resolves when cwd is
    the installing repo's own root, which this hermetic fixture is not.
    """
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    tail = ' "$@"' if forward_args else ""
    body = f'#!/bin/sh\nexec "{_fwd(sys.executable)}" "{_fwd(script)}"{tail}\n'
    hook_path = hooks_dir / hook_name
    hook_path.write_text(body, encoding="utf-8", newline="\n")
    try:
        st = os.stat(hook_path)
        os.chmod(hook_path, st.st_mode | 0o111)
    except OSError:
        pass


def _build_fixture_repo(tmp_path: Path) -> Path:
    """A realistic staged working tree (§ Resolved): `N_TRACKED_FILES` files,
    each with `N_HISTORY_REVISIONS` prior commits, on a `work/*` branch, with
    all three hooks installed and a local bare `origin` already tracking that
    branch (so the post-commit hook's sync push is a real, bounded,
    no-network fast-forward -- see module docstring's DETACHED AUTO-PUSH
    SUBTREE exclusion section).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/striker/c3-budget-fixture")
    _git(repo, "config", "user.email", "c3-budget@example.com")
    _git(repo, "config", "user.name", "c3-budget")

    names = [f"f{i:03d}.txt" for i in range(N_TRACKED_FILES)]
    for rev in range(N_HISTORY_REVISIONS):
        for name in names:
            (repo / name).write_text(f"history rev {rev} {name}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"history revision {rev}")

    bare = tmp_path / "origin.git"
    _git(repo, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "work/striker/c3-budget-fixture")

    _install_hook(repo, "pre-commit", _DETECT_ROLLBACK, forward_args=False)
    _install_hook(repo, "prepare-commit-msg", _PREPARE_COMMIT_MSG, forward_args=True)
    _install_hook(repo, "post-commit", _AUTO_PUSH, forward_args=True)
    return repo


def _write_driver(driver_path: Path, repo: Path, counter_path: Path, do_commit: bool) -> None:
    """Per-invocation wrapper: mutates `N_MODIFIED_PER_COMMIT` files to
    content that has never appeared in any historical blob for that path
    (rollback-safe) and never deletes anything (mass-deletion-safe), stages
    them, and -- when `do_commit` -- commits, which is what fires the three
    installed hooks. `do_commit=False` is the driver-residue calibration
    shape (module docstring's DRIVER RESIDUE section): identical file
    mutation and `git add`, no `git commit`, so its own cost isolates
    exactly what the full-commit measurement's driver overhead is.

    The counter file (outside the repo, never staged) makes every
    invocation's content distinct across `batched_process_time_ms`'s k
    identical-argv re-runs -- the same idempotent-fixture trap C1's own
    harness names ("regenerate the message file fresh per invocation").
    """
    commit_line = (
        'subprocess.run(["git", "commit", "-q", "-m", f"driver commit {n}"], '
        'cwd=repo, check=True, capture_output=True, env=env, **NO_WINDOW)\n'
        if do_commit
        else ""
    )
    script = f'''\
import os
import subprocess
import sys
from pathlib import Path

repo = r"{repo}"
counter_path = Path(r"{counter_path}")
n = int(counter_path.read_text()) if counter_path.exists() else 0
n += 1
counter_path.write_text(str(n))

for i in range({N_MODIFIED_PER_COMMIT}):
    Path(repo, f"f{{i:03d}}.txt").write_text(f"driver rev {{n}} file {{i}} -- never historical\\n")

env = dict(os.environ)
NO_WINDOW = {{"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}}
subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, env=env, **NO_WINDOW)
{commit_line}'''
    driver_path.write_text(script, encoding="utf-8")


def _measure_driver_residue(tmp_path: Path, repo: Path, env: dict) -> dict:
    driver = tmp_path / "driver_residue.py"
    counter = tmp_path / "residue_counter.txt"
    _write_driver(driver, repo, counter, do_commit=False)
    result = batched_process_time_ms(
        [sys.executable, str(driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert result["rc"] == 0, f"driver-residue calibration must exit 0: {result!r}"
    # Undo the staged mutations this calibration made -- the full-commit
    # measurement below needs a clean starting index, and this run never
    # committed anything to clean up after itself.
    _git(repo, "reset", "-q", "--hard", "HEAD", env=env)
    return result


def _measure_gate_alone(repo: Path, env: dict) -> dict:
    """Isolates the gate's (`detect-staged-rollback`) own process time
    against an equivalently-staged snapshot -- same read-only trampoline C4's
    own pin measures, never mutating the repo (module docstring's own
    "Read-and-report only" note), so it is safe to batch k times against one
    staged snapshot without re-staging between calls.
    """
    for i in range(N_MODIFIED_PER_COMMIT):
        (repo / f"f{i:03d}.txt").write_text(
            f"gate-calibration snapshot file {i} -- never historical\n", encoding="utf-8"
        )
    _git(repo, "add", "-A", env=env)
    result = batched_process_time_ms(
        [sys.executable, str(_DETECT_ROLLBACK), str(repo)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert result["rc"] == 0, (
        f"gate calibration must be a clean verdict (a blocked run costs "
        f"differently, § Corrections 5): {result!r}"
    )
    _git(repo, "reset", "-q", "--hard", "HEAD", env=env)
    return result


# ---------------------------------------------------------------------------
# Two numbers, not one -- see module docstring's "WHAT THIS FILE PINS".
# The RATCHET is the regression lock (green today). The AC TARGET is the
# budget the plan was written to reach (red today, `designed_red`).
# ---------------------------------------------------------------------------

#: AC1-comparable ratchet: full-commit process time, MINUS driver residue,
#: MINUS the gate's own (named-excluded) history-walk time. Derived from the
#: measured 1177.7ms (module docstring) with a 25% headroom band -- process
#: time drifts under the 50-70 concurrent sessions this box carries, and a
#: ratchet that fires on peer load trains everyone to ignore it. Lower this
#: whenever a cut lands; it is a floor, never a budget.
AC1_PROCESS_TIME_RATCHET_MS = 1475.0

#: AC2-comparable ratchet: full-commit spawn count, MINUS driver residue only
#: (the gate's own processes are NOT excluded here -- see module docstring:
#: AC2 excludes driver residue and the detached subtree, not the gate's fixed
#: process count, which does not scale with history depth the way its TIME
#: does). Conhost processes are COUNTED, per AC2's own instruction -- a
#: submission that hits a lower number by dropping CREATE_NO_WINDOW has
#: failed this pin, not passed it. Measured 37.625; +1 covers the retry-shaped
#: variance a spawn count actually has, which is far tighter than time's.
AC2_SPAWN_COUNT_RATCHET = 38.625

#: The plan's own AC1: everything on the commit path except the gate's history
#: walk under 500ms process time.
AC1_PROCESS_TIME_TARGET_MS = 500.0

#: The plan's own AC2: the same commit at <=12 processes, down from ~35.6.
AC2_SPAWN_COUNT_TARGET = 12.0


@pytest.fixture(scope="module")
def commit_path_measurement(tmp_path_factory):
    """One end-to-end measurement, shared by the ratchet and the target --
    building the 150-file/3-revision fixture and running k=8 real commits
    costs ~60s of box occupancy, and paying it twice to assert two numbers
    about the same run would be exactly the load this plan exists to cut.
    """
    _require_windows()
    tmp_path = tmp_path_factory.mktemp("commit_budget")

    repo = _build_fixture_repo(tmp_path)
    env = _env(COORDINATOR_AUTO_PUSH_SYNC="1")

    residue = _measure_driver_residue(tmp_path, repo, env)
    gate_alone = _measure_gate_alone(repo, env)

    driver = tmp_path / "driver_commit.py"
    counter = tmp_path / "commit_counter.txt"
    _write_driver(driver, repo, counter, do_commit=True)
    full = batched_process_time_ms(
        [sys.executable, str(driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert full["rc"] == 0, (
        f"full commit-path measurement must exit 0 (a hook that blocks or a "
        f"push that fails is not the shape this pin measures): {full!r}"
    )

    ac1_ms = full["process_time_ms"] - residue["process_time_ms"] - gate_alone["process_time_ms"]
    ac2_procs = full["procs_per_call"] - residue["procs_per_call"]
    detail = (
        f"raw full-commit total: {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs (k={K_INVOCATIONS}). "
        f"driver residue (excluded, both axes): {residue['process_time_ms']}ms / "
        f"{residue['procs_per_call']} procs. "
        f"gate history-walk (AC1 time exclusion only, NOT excluded from AC2's "
        f"spawn count -- see module docstring): {gate_alone['process_time_ms']}ms / "
        f"{gate_alone['procs_per_call']} procs. "
        f"AC1-comparable: {ac1_ms}ms. AC2-comparable: {ac2_procs} procs."
    )
    return {"ac1_ms": ac1_ms, "ac2_procs": ac2_procs, "detail": detail}


def test_commit_path_does_not_regress(commit_path_measurement):
    """The regression lock (AC5): the path may not get worse than the shape
    C1/C2/C4 left it in. This is NOT the budget -- see the target below.
    """
    m = commit_path_measurement
    assert m["ac1_ms"] <= AC1_PROCESS_TIME_RATCHET_MS, (
        f"commit path (gate history-walk and driver residue excluded, both "
        f"named per AC1/AC5) regressed past the "
        f"{AC1_PROCESS_TIME_RATCHET_MS}ms ratchet: {m['detail']}"
    )
    assert m["ac2_procs"] <= AC2_SPAWN_COUNT_RATCHET, (
        f"commit path (driver residue excluded per AC5; gate processes and "
        f"conhost processes are COUNTED, not excluded, per AC2) regressed past "
        f"the {AC2_SPAWN_COUNT_RATCHET}-process ratchet: {m['detail']}"
    )


@pytest.mark.designed_red
def test_commit_path_meets_the_plans_budget(commit_path_measurement):
    """RED BY DESIGN, and its failure text is the worklist. AC1/AC2 are the
    numbers the plan was written to reach; C1/C2/C4 cut real cost and did not
    reach them. Nothing in this file may be relaxed to make this green -- the
    gap closes by removing spawns (AC2's own instruction), and re-measuring
    once C16's rollback-check kill lands (module docstring).
    """
    m = commit_path_measurement
    assert m["ac1_ms"] <= AC1_PROCESS_TIME_TARGET_MS, (
        f"AC1 unmet: commit path costs {m['ac1_ms']}ms against a "
        f"{AC1_PROCESS_TIME_TARGET_MS}ms budget. {m['detail']}"
    )
    assert m["ac2_procs"] <= AC2_SPAWN_COUNT_TARGET, (
        f"AC2 unmet: commit path costs {m['ac2_procs']} processes against a "
        f"{AC2_SPAWN_COUNT_TARGET}-process budget. {m['detail']}"
    )


# ---------------------------------------------------------------------------
# DARWIN LEG (C7) -- separately identifiable from every test above by name
# (`..._darwin_...`), fixture instance, and constant namespace. Does not
# extend, parametrise, or share a measurement fixture with the Windows tests
# above; see module docstring's DARWIN LEG section for the measured baseline
# these constants are pinned against, and why this stays split from the
# Windows plan's own AC1/AC2/`designed_red` bookkeeping (AC21).
# ---------------------------------------------------------------------------

#: Measured (not derived, AC20): three independent campaign runs on this
#: box read AC1-analog p90 in [198.3, 240.6]ms -- wider spread than the
#: first two runs alone suggested, consistent with this box's 50-70
#: concurrent-session load norm (CLAUDE.md § Load norm: "process time
#: drifts under load"). Headroom is banded off the HIGHEST observed run
#: (240.6ms), not the first two: +25% rounds to 300.0ms. A regression LOCK,
#: not the plan's budget -- lower it whenever a cut lands, and re-band if a
#: future run exceeds this margin rather than treating one outlier as noise.
AC1_DARWIN_PROCESS_TIME_RATCHET_MS = 300.0

#: Measured: three runs read AC2-analog in [23.125, 23.375] procs -- spawn
#: count is far tighter than time's variance under load (module docstring's
#: own note, matching the Windows ratchet's rationale); +1 covers
#: retry-shaped variance, matching the Windows ratchet's own +1 convention.
AC2_DARWIN_SPAWN_COUNT_RATCHET = 24.375

#: The same repo-wide commit-path budget the Windows plan states as its own
#: AC1/AC2 (500ms / 12 procs) -- reused here as a comparison target only,
#: never as a write into that plan's AC bookkeeping (module docstring).
AC1_DARWIN_PROCESS_TIME_TARGET_MS = AC1_PROCESS_TIME_TARGET_MS
AC2_DARWIN_SPAWN_COUNT_TARGET = AC2_SPAWN_COUNT_TARGET


@pytest.fixture(scope="module")
def commit_path_measurement_darwin(tmp_path_factory):
    """Darwin's own end-to-end measurement -- a fresh fixture instance, never
    shared with `commit_path_measurement` above (module docstring: no
    parametrisation across platforms). Uses `batched_process_time_quantiles`
    (n=3, k=5) for the full-commit leg -- the "measure, report n/p50/p90,
    then pin" discipline AC20 requires for a discovered (not ported)
    baseline -- plus one k=8 `batched_process_time_ms` call for the spawn
    count, since quantiles only summarises process time.
    """
    _require_darwin()
    tmp_path = tmp_path_factory.mktemp("commit_budget_darwin")

    repo = _build_fixture_repo(tmp_path)
    env = _env(COORDINATOR_AUTO_PUSH_SYNC="1")

    residue = _measure_driver_residue(tmp_path, repo, env)
    gate_alone = _measure_gate_alone(repo, env)

    driver = tmp_path / "driver_commit.py"
    counter = tmp_path / "commit_counter.txt"
    _write_driver(driver, repo, counter, do_commit=True)

    full_quantiles = batched_process_time_quantiles(
        [sys.executable, str(driver)], k=K_INVOCATIONS, n=3, cwd=str(repo), env=env
    )

    full_procs = batched_process_time_ms(
        [sys.executable, str(driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert full_procs["rc"] == 0, (
        f"full commit-path spawn-count measurement must exit 0: {full_procs!r}"
    )

    ac1_p50_ms = full_quantiles["p50_ms"] - residue["process_time_ms"] - gate_alone["process_time_ms"]
    ac1_p90_ms = full_quantiles["p90_ms"] - residue["process_time_ms"] - gate_alone["process_time_ms"]
    ac2_procs = full_procs["procs_per_call"] - residue["procs_per_call"]
    detail = (
        f"full-commit quantiles: n={full_quantiles['n']} k={full_quantiles['k']} "
        f"p50={full_quantiles['p50_ms']}ms p90={full_quantiles['p90_ms']}ms "
        f"samples={full_quantiles['samples']}. "
        f"full-commit spawn count (k={full_procs['k']}): {full_procs['procs_per_call']} procs. "
        f"driver residue (excluded, both axes): {residue['process_time_ms']}ms / "
        f"{residue['procs_per_call']} procs. "
        f"gate history-walk (AC1-analog time exclusion only): "
        f"{gate_alone['process_time_ms']}ms / {gate_alone['procs_per_call']} procs. "
        f"AC1-analog: p50={ac1_p50_ms}ms p90={ac1_p90_ms}ms. AC2-analog: {ac2_procs} procs."
    )
    return {"ac1_p50_ms": ac1_p50_ms, "ac1_p90_ms": ac1_p90_ms, "ac2_procs": ac2_procs, "detail": detail}


def test_commit_path_darwin_does_not_regress(commit_path_measurement_darwin):
    """The macOS regression lock (AC20/AC21): may not get worse than the
    MEASURED baseline this chunk discovered (module docstring). Not the
    Windows plan's ratchet, not a contribution to its AC1/AC2 numbers.
    """
    m = commit_path_measurement_darwin
    assert m["ac1_p90_ms"] <= AC1_DARWIN_PROCESS_TIME_RATCHET_MS, (
        f"macOS commit path (gate history-walk and driver residue excluded) "
        f"regressed past the {AC1_DARWIN_PROCESS_TIME_RATCHET_MS}ms ratchet: "
        f"{m['detail']}"
    )
    assert m["ac2_procs"] <= AC2_DARWIN_SPAWN_COUNT_RATCHET, (
        f"macOS commit path (driver residue excluded; gate processes counted) "
        f"regressed past the {AC2_DARWIN_SPAWN_COUNT_RATCHET}-process ratchet: "
        f"{m['detail']}"
    )


def test_commit_path_darwin_meets_the_process_time_budget(commit_path_measurement_darwin):
    """macOS's process-time leg against the repo-wide 500ms budget -- GREEN
    on this box (module docstring's REPORTED FINDING): no conhost, no
    job-object overhead makes this leg materially cheaper than Windows on
    the identical fixture. Not `designed_red` -- unlike its spawn-count
    sibling below, this axis actually meets the budget today.
    """
    m = commit_path_measurement_darwin
    assert m["ac1_p90_ms"] <= AC1_DARWIN_PROCESS_TIME_TARGET_MS, (
        f"macOS commit path costs {m['ac1_p90_ms']}ms (p90) against a "
        f"{AC1_DARWIN_PROCESS_TIME_TARGET_MS}ms budget. {m['detail']}"
    )


@pytest.mark.designed_red
def test_commit_path_darwin_meets_the_spawn_count_budget(commit_path_measurement_darwin):
    """RED BY DESIGN on this box, and its own worklist -- macOS still spawns
    roughly double the repo-wide 12-process budget (module docstring's
    REPORTED FINDING: spawn count, not process time, is macOS's real gap).
    This is a macOS-native finding, not the Windows plan's AC2 restated --
    nothing here edits that plan or its own `designed_red` test.
    """
    m = commit_path_measurement_darwin
    assert m["ac2_procs"] <= AC2_DARWIN_SPAWN_COUNT_TARGET, (
        f"macOS commit path costs {m['ac2_procs']} processes against a "
        f"{AC2_DARWIN_SPAWN_COUNT_TARGET}-process budget. {m['detail']}"
    )
