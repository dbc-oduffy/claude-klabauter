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

ONE NAMED EXCLUSION, REPORTED alongside the passing number, never in place
of it (AC1's own requirement):

  (The GATE HISTORY WALK exclusion this section used to describe --
  subtracting the pre-commit rollback-detector gate's own process time from
  the full-commit total -- no longer applies: that gate
  (`coordinator_core.ops.detect_staged_rollback`) and its installer are
  deleted, 2026-08-25, "the staged rollback gate dies without blocking a
  commit"; claude-klabauter ends with no pre-commit hook. This fixture no longer
  installs one, so the full-commit total has nothing left to exclude on that
  axis -- see `docs/research/spike-verdicts/2026-08-21-staged-rollback-
  verdict-within-budget.md` for the now-historical record of why that gate's
  own cost could not be cut further while it existed.)

  (The DETACHED AUTO-PUSH SUBTREE exclusion this section used to describe --
  forcing `COORDINATOR_AUTO_PUSH_SYNC=1` so the post-commit hook's push ran
  in-process instead of detached, then including that synchronous push's
  real cost in the pinned number -- is VOID, not merely superseded (D2, D5).
  `coordinator/bin/coordinator-auto-push.py` -- the post-commit hook's
  target script -- is deleted (2026-08-30) and the per-commit auto-push
  mechanism is gravestoned with no installer that could ever restore it
  (`coordinator_core/hooks/auto_push.py`'s own docstring records the
  deletion). This fixture no longer installs a post-commit hook at all (D5:
  dropped, not repointed -- there is nothing left to repoint it to), so there
  is no push, synchronous or detached, on the commit path to exclude, avoid,
  or force. `COORDINATOR_AUTO_PUSH_SYNC=1`, the fixture's local bare `origin`
  and its tracking branch are SCENERY: kept only so the fresh figures stay
  comparable to the 2026-08-21 record on the staged-shape axis (D5, F6), not
  because they still gate anything this file measures.)

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
because at that moment claude-klabauter's own working tree carried another
session's in-flight edit to `coordinator_core/ops/detect_staged_rollback.py`
(a `NameError` from a partially-applied cut). Engine content there was
verified equivalent to `HEAD` (publish-time identifier renames only).

WHY THE RATCHET IS NOT YET THE FINAL PIN (historical numbers above, superseded
2026-08-25). The exact-blob rollback check -- the term AC1 named as its
exclusion, and the term C5 priced at 1184ms and returned `not-viable` on --
was DELETED under a PM ruling by C16 of `docs/plans/2026-08-21-the-cli
-bootstrap-tax-dies-at-the-interpreter-floor.md` (check 2, the mass-deletion
tripwire, was NOT cut). The whole gate module (`detect_staged_rollback.py`)
and its installer were then deleted outright, 2026-08-25 ("the staged
rollback gate dies without blocking a commit"), so the excluded term this
file used to subtract no longer applies at all -- this fixture no longer
installs a pre-commit hook, and the numbers above are a historical record of
the measurement, not the current shape. Re-measure and lower the ratchet on
the next pass through this file; do not treat today's figure as the budget
C3 was asked to pin.

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

WHAT THIS FILE PINS, AND WHAT IT DOES NOT (2026-08-25, on DR-356, at
pickup). `docs/decisions/DR-356-what-a-commit-path-budget-measures.md`
rules the precise question this file's own numbers answer -- "given an op
that commits, which processes count toward its budget?" -- as a table, not
a feeling: the op's own spawns and git's own work for the commit itself are
IN; `pre-commit`, `prepare-commit-msg`, `post-commit`, and anything they
spawn are OUT, measured and reported alongside, never in place of, the
budgeted figure. This file's own subject is the commit path with hooks IN
(`_build_fixture_repo`'s one surviving `_install_hook` call, `prepare-commit-msg`
-- the pre-commit gate and the post-commit push hook are both deleted from the
fixture, D5) -- the raw full-commit total this file's AC1/AC2 pins are drawn
against includes the hook chain DR-356 rules OUT of an op's own budget. That
makes this file's subject DISTINCT from the shape DR-356 rules an op's own
budget is assessed against; nothing here is re-columned for that reason, and
every threshold and both `designed_red` markers stand exactly as pinned.
`_measure_driver_residue`, this file's own named-exclusion mechanism (see
above), is the pattern DR-356's two-figure reporting shape generalises --
this file got there first, and this note is the record saying so.

FIXTURE REPAIR (D5, 2026-09-24, at this pickup). The fixture used to install a
`post-commit` hook pointed at `coordinator/bin/coordinator-auto-push.py`,
which does not exist -- `_install_hook` writes an unchecked `exec` line for
it and git ignores `post-commit`'s exit code, so every prior run of this
fixture measured one hook that started an interpreter and died on a missing
file, plus one hook that actually ran. That hook is DROPPED here, not
repointed -- there is nothing to repoint it to (§ above). The surviving
`prepare-commit-msg` hook now writes a zero-cost sentinel line (a shell
builtin `>>` append, no spawned process) before its `exec`, and
`commit_path_measurement`/`commit_path_measurement_darwin` assert the
sentinel's line count against the number of hook-firing invocations before
trusting `ac1_ms`/`ac2_procs` -- a missing or no-op hook now fails the
fixture instead of silently producing a plausible number (D5's
executed-assertion). This repair changes the hook set and adds an assertion
only; `_build_fixture_repo`'s measured shape (`N_TRACKED_FILES`,
`N_HISTORY_REVISIONS`, `N_MODIFIED_PER_COMMIT`) is untouched and not
re-derived (§ Resolved, ratified).

DARWIN STALENESS (D5, F3, option (b)). Dropping the post-commit hook removes
a counted leg from `AC1_DARWIN_PROCESS_TIME_RATCHET_MS` and
`AC2_DARWIN_SPAWN_COUNT_RATCHET` (both measured 2026-08-22 against the
pre-repair fixture, which installed that hook). Those two constants and
their `designed_red` markers are left EXACTLY as they stand on this pass --
touching them would need a macOS box this chunk's dispatch precondition
(D4) does not route to, and a downward re-pin taken on a box the fixture was
never run on is the same un-run-fixture defect D5 exists to end. Dropping a
counted leg only makes the darwin figures LOWER, so the existing constants
are now stale-over-permissive (a loose lock, never a red one) -- safe to
leave stale, unlike a constant that could now be too tight. The owed
re-measure is filed as one `state/debt-backlog/` entry against
`docs/plans/2026-08-22-the-brightlines-instrument-exists-on-the-fleet-floor.md`
C7, the constants' own owning chunk.
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
_PREPARE_COMMIT_MSG = _BIN_DIR / "coordinator-prepare-commit-msg.py"
# _AUTO_PUSH (coordinator-auto-push.py) is gone: the post-commit hook that
# pointed at it is dropped, not repointed (D5) -- there is nothing left to
# repoint it to, and no caller in this file needs the path anymore.

_CLAUDE_KLABAUTER_ROOT = str(Path(__file__).resolve().parents[2].parent)

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
    spawned child's real machine-local registry or `.claude-klabauter-live-root` pointer
    is reachable -- same shape as `test_detect_staged_rollback_spawn_
    budget.py::_env`.
    """
    base = dict(os.environ)
    base.setdefault("COORDINATOR_ENGINE_ROOT", _CLAUDE_KLABAUTER_ROOT)
    base.update(overrides)
    return base


def _install_hook(
    repo: Path,
    hook_name: str,
    script: Path,
    forward_args: bool,
    sentinel: Path | None = None,
) -> None:
    """Write a minimal, bash-free `.git/hooks/<hook_name>` that `exec`s
    `script` by ABSOLUTE path -- the same shape `git_hook_install.py`'s own
    shim exec-line uses (`exec "$_PY" "$SCRIPT" "$@"`), simplified to a fixed
    interpreter (`sys.executable`) rather than that module's python3/python/py
    probe chain, since this fixture controls its own interpreter and does not
    need to rediscover it. Absolute-path invocation is load-bearing (C4's own
    § Corrections 5 finding): a relative baked path only resolves when cwd is
    the installing repo's own root, which this hermetic fixture is not.

    `sentinel`, when given, adds one line BEFORE the `exec` -- a shell
    builtin `echo ... >> <sentinel>` append (D5's executed-assertion). `echo`
    with no flags and a plain redirect is a POSIX shell builtin, so this adds
    no process to the measured window (D5's zero-cost requirement); the
    caller counts lines in `sentinel` after the run and compares against the
    number of hook-firing invocations to prove the hook actually ran, rather
    than trusting a plausible-looking number from a hook that silently
    short-circuited or died on a missing target.
    """
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    tail = ' "$@"' if forward_args else ""
    sentinel_line = f'echo x >> "{_fwd(sentinel)}"\n' if sentinel is not None else ""
    body = f'#!/bin/sh\n{sentinel_line}exec "{_fwd(sys.executable)}" "{_fwd(script)}"{tail}\n'
    hook_path = hooks_dir / hook_name
    hook_path.write_text(body, encoding="utf-8", newline="\n")
    try:
        st = os.stat(hook_path)
        os.chmod(hook_path, st.st_mode | 0o111)
    except OSError:
        pass


def _build_fixture_repo(tmp_path: Path, sentinel: Path) -> Path:
    """A realistic staged working tree (§ Resolved): `N_TRACKED_FILES` files,
    each with `N_HISTORY_REVISIONS` prior commits, on a `work/*` branch, with
    the ONE surviving hook installed (D5: pre-commit is gone -- the gate it
    ran, `detect_staged_rollback`, and its installer are deleted, 2026-08-25;
    post-commit is DROPPED here -- its target, `coordinator-auto-push.py`, is
    deleted and the per-commit auto-push mechanism is gravestoned, so there
    is nothing to repoint post-commit to; claude-klabauter ends with no pre-commit
    hook and this fixture no longer installs a post-commit one either). The
    local bare `origin` and its tracking branch are kept as SCENERY (D5, F6)
    -- inert now that there is no push hook to make them relevant, retained
    only so the fresh figures stay comparable to the 2026-08-21 record on the
    staged-shape axis.

    `sentinel` is the file `prepare-commit-msg`'s installed copy appends one
    line to per invocation (D5's executed-assertion) -- passed through to
    `_install_hook` so the caller can prove the hook actually ran before
    trusting any number derived from it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/machine-a/c3-budget-fixture")
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
    _git(repo, "push", "-q", "-u", "origin", "work/machine-a/c3-budget-fixture")

    _install_hook(
        repo, "prepare-commit-msg", _PREPARE_COMMIT_MSG, forward_args=True, sentinel=sentinel
    )
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


# ---------------------------------------------------------------------------
# Two numbers, not one -- see module docstring's "WHAT THIS FILE PINS".
# The RATCHET is the regression lock (green today). The AC TARGET is the
# budget the plan was written to reach (red today, `designed_red`).
# ---------------------------------------------------------------------------

#: AC1-comparable ratchet: full-commit process time, MINUS driver residue.
#: Historically also excluded the pre-commit rollback gate's own
#: (named-excluded) history-walk time; that gate and its installer are
#: deleted (2026-08-25) and this fixture no longer installs it, so there is
#: nothing left on that axis to exclude -- see module docstring. Derived from
#: the historical measured 1177.7ms (module docstring) with a 25% headroom
#: band -- process time drifts under the 50-70 concurrent sessions this box
#: carries, and a ratchet that fires on peer load trains everyone to ignore
#: it. Lower this whenever a cut lands; it is a floor, never a budget.
AC1_PROCESS_TIME_RATCHET_MS = 1475.0

#: AC2-comparable ratchet: full-commit spawn count, MINUS driver residue
#: only (see module docstring: AC2 excludes driver residue and the detached
#: subtree). Conhost processes are COUNTED, per AC2's own instruction -- a
#: submission that hits a lower number by dropping CREATE_NO_WINDOW has
#: failed this pin, not passed it. Measured (historically, with the
#: since-deleted pre-commit gate installed) 37.625; +1 covers the
#: retry-shaped variance a spawn count actually has, which is far tighter
#: than time's.
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

    sentinel = tmp_path / "prepare_commit_msg.sentinel"
    repo = _build_fixture_repo(tmp_path, sentinel)
    env = _env(COORDINATOR_AUTO_PUSH_SYNC="1")

    residue = _measure_driver_residue(tmp_path, repo, env)

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

    # D5's executed-assertion: the residue calibration above never commits
    # (do_commit=False), so it cannot fire prepare-commit-msg -- only the
    # K_INVOCATIONS real commits above can. A short-circuited or dead hook
    # produces fewer lines than that; a fixture bug producing more would be
    # just as untrustworthy. Either way this is not a measurement.
    sentinel_lines = sentinel.read_text(encoding="utf-8").splitlines() if sentinel.exists() else []
    assert len(sentinel_lines) == K_INVOCATIONS, (
        f"prepare-commit-msg did not fire exactly {K_INVOCATIONS} times "
        f"({len(sentinel_lines)} observed) -- a hook that did not run is not "
        f"a measurement (D5): {full!r}"
    )

    ac1_ms = full["process_time_ms"] - residue["process_time_ms"]
    ac2_procs = full["procs_per_call"] - residue["procs_per_call"]
    detail = (
        f"raw full-commit total: {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs (k={K_INVOCATIONS}). "
        f"driver residue (excluded, both axes): {residue['process_time_ms']}ms / "
        f"{residue['procs_per_call']} procs. "
        f"no gate history-walk term (the pre-commit rollback gate and its "
        f"installer are deleted, 2026-08-25 -- this fixture installs no "
        f"pre-commit hook, so there is nothing left to exclude on that axis). "
        f"AC1-comparable: {ac1_ms}ms. AC2-comparable: {ac2_procs} procs."
    )
    return {"ac1_ms": ac1_ms, "ac2_procs": ac2_procs, "detail": detail}


def test_commit_path_does_not_regress(commit_path_measurement):
    """The regression lock (AC5): the path may not get worse than the shape
    C1/C2/C4 left it in. This is NOT the budget -- see the target below.
    """
    m = commit_path_measurement
    assert m["ac1_ms"] <= AC1_PROCESS_TIME_RATCHET_MS, (
        f"commit path (driver residue excluded per AC5; no pre-commit gate "
        f"to exclude, see module docstring) regressed past the "
        f"{AC1_PROCESS_TIME_RATCHET_MS}ms ratchet: {m['detail']}"
    )
    assert m["ac2_procs"] <= AC2_SPAWN_COUNT_RATCHET, (
        f"commit path (driver residue excluded per AC5; conhost processes "
        f"are COUNTED, not excluded, per AC2) regressed past "
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

#: STALE-OVER-PERMISSIVE, LEFT AS-IS (D5, F3 option (b), 2026-09-24). Both
#: darwin constants below were measured 2026-08-22 against a fixture that
#: installed a `post-commit` hook this file's D5 repair now DROPS. Dropping
#: a counted leg only makes the fresh darwin figures LOWER, so this constant
#: is now a looser lock than a fresh measurement would set, never a
#: tighter/red one -- safe to leave stale on that basis. Untouched here
#: because re-measuring needs a macOS box and this chunk's dispatch
#: precondition (D4) does not route to one; the owed re-measure is filed
#: against `docs/plans/2026-08-22-the-brightlines-instrument-exists-on-the-
#: fleet-floor.md` C7 in `state/debt-backlog/`, not resolved here.
#:
#: Measured (not derived, AC20): three independent campaign runs on this
#: box read AC1-analog p90 in [198.3, 240.6]ms -- wider spread than the
#: first two runs alone suggested, consistent with this box's 50-70
#: concurrent-session load norm (CLAUDE.md § Load norm: "process time
#: drifts under load"). Headroom is banded off the HIGHEST observed run
#: (240.6ms), not the first two: +25% rounds to 300.0ms. A regression LOCK,
#: not the plan's budget -- lower it whenever a cut lands, and re-band if a
#: future run exceeds this margin rather than treating one outlier as noise.
AC1_DARWIN_PROCESS_TIME_RATCHET_MS = 300.0

#: STALE-OVER-PERMISSIVE, LEFT AS-IS -- see the note above this block; the
#: same D5/F3 reasoning applies to this constant on the spawn-count axis.
#:
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

    sentinel = tmp_path / "prepare_commit_msg.sentinel"
    repo = _build_fixture_repo(tmp_path, sentinel)
    env = _env(COORDINATOR_AUTO_PUSH_SYNC="1")

    residue = _measure_driver_residue(tmp_path, repo, env)

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

    # D5's executed-assertion: `full_quantiles` fires the hook
    # n*k times and `full_procs` fires it k more (the residue calibration
    # above never commits, so it contributes none). A short-circuited or
    # dead hook produces a different count; either way, not a measurement.
    expected_invocations = full_quantiles["n"] * full_quantiles["k"] + full_procs["k"]
    sentinel_lines = sentinel.read_text(encoding="utf-8").splitlines() if sentinel.exists() else []
    assert len(sentinel_lines) == expected_invocations, (
        f"prepare-commit-msg did not fire exactly {expected_invocations} "
        f"times ({len(sentinel_lines)} observed) -- a hook that did not run "
        f"is not a measurement (D5)."
    )

    ac1_p50_ms = full_quantiles["p50_ms"] - residue["process_time_ms"]
    ac1_p90_ms = full_quantiles["p90_ms"] - residue["process_time_ms"]
    ac2_procs = full_procs["procs_per_call"] - residue["procs_per_call"]
    detail = (
        f"full-commit quantiles: n={full_quantiles['n']} k={full_quantiles['k']} "
        f"p50={full_quantiles['p50_ms']}ms p90={full_quantiles['p90_ms']}ms "
        f"samples={full_quantiles['samples']}. "
        f"full-commit spawn count (k={full_procs['k']}): {full_procs['procs_per_call']} procs. "
        f"driver residue (excluded, both axes): {residue['process_time_ms']}ms / "
        f"{residue['procs_per_call']} procs. "
        f"no gate history-walk term (the pre-commit rollback gate and its "
        f"installer are deleted, 2026-08-25 -- this fixture installs no "
        f"pre-commit hook). "
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
        f"macOS commit path (driver residue excluded; no pre-commit gate to "
        f"exclude, see module docstring) regressed past the "
        f"{AC1_DARWIN_PROCESS_TIME_RATCHET_MS}ms ratchet: {m['detail']}"
    )
    assert m["ac2_procs"] <= AC2_DARWIN_SPAWN_COUNT_RATCHET, (
        f"macOS commit path (driver residue excluded) regressed past the "
        f"{AC2_DARWIN_SPAWN_COUNT_RATCHET}-process ratchet: {m['detail']}"
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


# ---------------------------------------------------------------------------
# Platform-independent unit test over the sentinel/hook-shim helper itself
# (D5, Review: coordinator-staff-eng F4). Every test above is gated by
# `_require_windows()`/`_require_darwin()`, so a linux executor landing step
# 0 alone (D4's carve-out) would otherwise get a collect-and-skip with the
# new sentinel logic never executed on any platform. This test writes a hook
# body to a temp dir and reads it back -- no job object, no `_require_*` --
# so the repair is provably exercised regardless of which box lands step 0.
# ---------------------------------------------------------------------------


def test_install_hook_sentinel_precedes_exec_and_costs_no_process(tmp_path):
    """The sentinel line must run BEFORE `exec` (else it never runs, since
    `exec` replaces the shell process) and must be a shell builtin, not a
    spawned helper (D5's zero-cost requirement) -- checked by reading the
    written hook body back, never by executing it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    script = tmp_path / "target.py"

    _install_hook(repo, "prepare-commit-msg", script, forward_args=True, sentinel=sentinel)

    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    lines = [ln for ln in hook_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    sentinel_idx = next(i for i, ln in enumerate(lines) if _fwd(sentinel) in ln)
    exec_idx = next(i for i, ln in enumerate(lines) if ln.startswith("exec "))
    assert sentinel_idx < exec_idx, "sentinel append must precede exec, or it never runs"

    sentinel_line = lines[sentinel_idx]
    assert sentinel_line.startswith("echo "), (
        f"sentinel line must be a plain `echo` builtin (no spawned helper): {sentinel_line!r}"
    )
    assert ">>" in sentinel_line, f"sentinel line must APPEND, not overwrite: {sentinel_line!r}"


def test_install_hook_without_sentinel_writes_no_sentinel_line(tmp_path):
    """`sentinel=None` (every other `_install_hook` caller) must not write an
    append line at all -- the sentinel is opt-in, not a default cost added
    to every installed hook.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    script = tmp_path / "target.py"

    _install_hook(repo, "prepare-commit-msg", script, forward_args=True)

    hook_path = repo / ".git" / "hooks" / "prepare-commit-msg"
    lines = [ln for ln in hook_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert not any(">>" in ln for ln in lines), "no sentinel requested, none should be written"
    assert lines[-1].startswith("exec "), "the only content line must be the exec"
