"""
coordinator_core.benchmarks.tests.test_process_time_posix

C3 (docs/plans/2026-08-22-the-brightlines-instrument-exists-on-the-fleet-
floor.md): the test surface for `coordinator_core.benchmarks.process_time`'s
Darwin half -- negative spec, triangulated counts, and this file's own cost.
Darwin-only, the way the tree's existing job-object suites skip off
non-Windows: every test below calls `_require_darwin()` first and the
module is a documented no-op collection on Linux/Windows.

WHY TRIANGULATION, NOT A SINGLE REPRODUCTION (AC11/AC12). A single fixture
proves nothing about a x2 (or x0.5) defect -- a doubled count on a fixture
whose true count is even is invisible. Three fixtures, independently
DERIVED (never observed-then-blessed), close that hole:

  1. A 19-process nested `/bin/sh` chain. 19 is PRIME: a doubling defect
     reads 38, a halving defect reads 9.5, neither is a number this
     fixture's own construction could produce honestly. Built as a
     self-recursing shell script (`chain.sh` below) rather than nested
     `-c` quoting -- quoting depth 19 explodes exponentially (verified:
     `OSError: [Errno 7] Argument list too long` at depth 19 with naive
     `shlex.quote` nesting) and a recursive `/bin/sh "$0" $((n-1))` script
     sidesteps it entirely while keeping the count exactly `n`.
  2. `python -> git --version`, the oracle at 2.0. Derived, not measured
     independently: the now-deleted `test_detect_staged_rollback_spawn_
     budget.py`'s own Windows decomposition of a suppressed git spawn was
     `1 interpreter + 1 git + 1 conhost` -- `conhost.exe` is a Windows-only
     console allocation with no Darwin analogue (this module's own
     docstring never mentions one; Darwin's `_darwin_one_invocation` has no
     console-suppression step to allocate one for), so the Darwin
     prediction is that decomposition minus the console term: 2.0, not the
     doubled 4.
  3. A deep chain at depth 11 (also prime, so it cannot alias with a
     doubling/halving defect against fixture 1 or itself either).

BINARIES ARE PINNED (per dispatch brief). `sys.executable` is used for the
"interpreter" leg rather than a re-resolved `python3` -- `/usr/bin/python3`
on macOS can be an xcode-select stub that execs a different real
interpreter, which would silently turn the "1 interpreter" leg into 2 for a
reason that has nothing to do with this instrument. Similarly the git
binary is resolved once via `shutil.which("git")` and asserted non-None
before use, rather than assuming `/usr/bin/git` -- Homebrew git is a
different binary on a box that has it installed ahead of the system one on
`PATH`, and the fixture must count whichever git actually runs, not the
one the DR-344 verdict happened to invoke.

AC6'S SEAM. `NOTE_TRACK` (ENOTSUP on this kernel, per `process_time.py`'s
own comment) is not available as a test seam -- C1 forbids using it at
all -- so this file drives the ESRCH path a different way: reap a real
child via a direct `os.waitpid` (so the pid is genuinely, unambiguously
already dead) and monkeypatch `_posix_spawnp_suspended` to hand that
reaped pid back as the "spawned" root. Registering `EVFILT_PROC` on it
fails with ESRCH; verified live (this file's own construction) that the
observable failure is `_darwin_one_invocation` raising -- concretely a
`ProcessLookupError` from the subsequent `os.kill` on the already-reaped
pid, still an unambiguous raise, never a silent `procs_per_call == 1`.

AC4'S HALF -- SCOPING, NOT GUARD-FIRING. This file drives 200ms of
unrelated CPU through a DETACHED, double-forked noise process (see
`_spawn_detached_noise_and_reap_first_child` below) concurrently with a
measured invocation, and asserts the measured `process_time_ms` does not
move beyond a stated tolerance. A same-process, different-thread
`Popen.wait()` (the module docstring's own trap 4 shape, pre-fix) was
tried first and rejected here: `_darwin_one_invocation`'s own pre/post
`proc_listchildpids(os.getpid())` purity assertion -- part of C1's fix,
not a gap in it -- correctly REFUSES to measure at all while any ambient
child of the measuring process exists, so a same-process noise child
cannot be used as this test's seam without fighting an invariant the
module deliberately enforces. Double-forking detaches the noise process
from the measuring process's own child list entirely (the immediate child
is reaped synchronously before measurement starts; the grandchild that
does the actual CPU burn is orphaned and re-parented outside this
process's tree), which is what makes it usable as a seam here while still
being genuine, unrelated, concurrent CPU load on the box. This is
deliberately NOT an assertion that some guard fired -- that shape is
satisfiable by a guard that fires on nothing -- it is a direct assertion
on the number the primitive returns.

LOAD VARIANCE (per dispatch brief, 2026-08-01-an-executors-measured-
number-is-a-claim-about-its-machine-not-the-change). Spawn COUNT is
load-invariant and asserted exactly everywhere in this file. Process TIME
is not bit-stable, so every process-time assertion here carries an
explicit `n` and, where a delta claim is being made (AC4), an interleaved
control rather than a bare before/after -- never a single sample pinned to
a spike's own figure (2026-08-21-a-zero-with-no-denominator-is-not-a-
measurement).

AC17, THIS FILE'S OWN COST. Every test here is real-process-spawning:
`pytestmark` below carries both `spawns_process` (the spawn ratchet,
`coordinator_core/tests/test_no_new_spawning_tests.py`, Rule 2) and
`cadence` (Rule 4 -- this file must not land on the per-commit tier).
Re-measured on this box (`pytest ... -q`, this file alone, n=5 runs, after
adding the 500-iteration reap adversary): 8 tests passing each run, pytest-
reported wall times 1.53/1.48/1.48/1.50/1.67s (min 1.48s, max 1.67s, spread
0.19s -- NOT the near-zero 0.01s spread an earlier n=3 measurement in this
docstring previously claimed, which did not survive independent
re-measurement). Pass/fail alone does not distinguish which of these 5 runs
took the reap adversary's own ~20% early-raise path (AC7, below) from which
completed all 500 forked-and-reaped children -- the adversary's `except
RuntimeError` branch returns a pass either way, so this figure honestly
cannot and does not claim to isolate that variance component; the 0.19s
spread across 5 runs is consistent with, not contradicted by, a residual
early-raise rate firing on some runs and not others. The two
triangulated-chain fixtures alone spawn
19*3 + 11*3 = 90 `/bin/sh` processes (k=3 each); the reap adversary
(AC17's own gap, k=1 -- see below) adds 501 more in a single invocation;
the oracle fixture and the AC4/AC6/AC18 probes add on the order of
another 30-40 light `sys.executable`/`git` processes -- low-to-mid
hundreds of processes total for the whole file, the reap adversary now
being the single largest contributor. Comfortably cadence-tier work, not
fast-tier: a suite driving a 19-process tree, a 501-process reap
adversary, and a 200ms detached CPU-burn noise fixture per file run is
not something 50-70 concurrent LLM sessions should each be paying on
every commit.

XDIST SAFETY, STATED EXPLICITLY SO NOBODY RE-ADDS A SERIAL MARKER LATER.
This file is xdist-SAFE. `_darwin_one_invocation` reaps each invocation's
own root via a pid-keyed `os.wait4()` (C1's own structural fix for the
`getrusage(RUSAGE_CHILDREN)` process-wide contamination trap) -- every
measurement here is scoped to a pid this process itself owns, so two
xdist workers each running this file concurrently cannot observe or
contaminate each other's counts. No `pytest.mark.serial`-shaped marker
exists in this tree's marker registry (`pyproject.toml`) and none is
added here.

Spec backlink: docs/plans/2026-08-22-the-brightlines-instrument-exists-on-
the-fleet-floor.md, chunk C3.
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys

import pytest

from coordinator_core.benchmarks import process_time
from coordinator_core.benchmarks.process_time import IS_DARWIN, batched_process_time_ms
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DR344_BRIGHTLINE_MS = 500.0
"""DR-344's own ratified 500ms process-time budget
(docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md) --
AC18 measures this instrument's own overhead against this same number,
not a tighter, locally-invented one."""

_CHAIN_SCRIPT = """\
#!/bin/sh
n=$1
if [ "$n" -gt 1 ]; then
  /bin/sh "$0" $((n-1))
fi
"""
# A self-recursing shell script, not nested `-c` quoting: nesting depth 19
# via `shlex.quote`-wrapped `-c` strings grows the argv exponentially and
# fails outright ("Argument list too long") well before depth 19 (verified
# during this file's own construction) -- `/bin/sh "$0" $((n-1))` spawns
# exactly one new process per decrement, giving a count that IS `n`, no
# quoting blow-up, and no ambiguity about what was actually counted.


def _require_darwin() -> None:
    if not IS_DARWIN:
        pytest.skip("process_time's kqueue/EVFILT_PROC primitive is Darwin-only")


@pytest.fixture(scope="module")
def chain_script(tmp_path_factory):
    # Gated behind the module's own Darwin skip (Finding 4, S3-tests
    # review): a git-less non-Darwin box should skip this Darwin-only file
    # cleanly, not fail collection over dead setup work for tests that
    # would skip anyway.
    _require_darwin()
    path = tmp_path_factory.mktemp("process_time_posix") / "chain.sh"
    path.write_text(_CHAIN_SCRIPT)
    path.chmod(0o755)
    return str(path)


@pytest.fixture(scope="module")
def git_binary():
    # Gated behind the module's own Darwin skip (Finding 4, S3-tests
    # review): a git-less non-Darwin box should skip cleanly rather than
    # fail collection on this fixture's own assert.
    _require_darwin()
    git_path = shutil.which("git")
    assert git_path, "no git binary resolvable on PATH -- fixture cannot pin a binary that isn't there"
    return git_path


# ---------------------------------------------------------------------------
# AC10 -- negative spec: a wall-clock value smuggled under the process-time
# key must fail this test.
# ---------------------------------------------------------------------------


def test_process_time_key_is_not_wall_clock():
    """`sleep 0.2` is large wall, near-zero CPU -- a wall-clock value
    reintroduced behind `process_time_ms` (DR-344 names this exact
    substitution) would read ~200ms here; the real process-time primitive
    reads a few ms of scheduling/exec overhead only. The two are asserted
    to differ by at least an order of magnitude, not merely "process_time
    < wall" (which a slightly-mismeasured-but-still-wall-clock-shaped value
    could also satisfy)."""
    _require_darwin()
    result = batched_process_time_ms(["/bin/sh", "-c", "sleep 0.2"], k=1)
    assert result["rc"] == 0, f"sleep fixture did not exit 0: {result!r}"
    assert result["wall_ms"] >= 150.0, (
        f"sleep 0.2 fixture's own wall_ms ({result['wall_ms']}) is implausibly "
        "low for this assertion to be meaningful -- fixture itself is suspect"
    )
    assert result["process_time_ms"] <= result["wall_ms"] / 10.0, (
        f"process_time_ms ({result['process_time_ms']}) is within an order of "
        f"magnitude of wall_ms ({result['wall_ms']}) on a near-zero-CPU sleep "
        "fixture -- this is the exact shape of a wall-clock value smuggled "
        "in behind the process-time key that DR-344 forbids"
    )


# ---------------------------------------------------------------------------
# AC11/AC12 -- triangulated exact counts across three independently-derived
# fixtures, chosen so a x2 (or x0.5) defect cannot hide behind any one of
# them (module docstring).
# ---------------------------------------------------------------------------


def test_nested_shell_chain_count_is_exactly_prime_nineteen(chain_script):
    """The key triangulation leg: 19 is PRIME, so a doubling defect (38) or
    a halving defect (9.5) cannot alias with the true count -- unlike an
    even fixture, where a x2 defect is invisible."""
    _require_darwin()
    result = batched_process_time_ms(["/bin/sh", chain_script, "19"], k=3)
    # `rc` reports only the LAST of the k=3 invocations by primitive contract
    # (process_time.py) -- this check cannot see an earlier invocation's rc
    # through the current API (Finding 1, S3-tests review). Kept at k=3
    # rather than dropped to k=1: the exact `== 19.0` equality below is what
    # actually carries the rest -- an off-invocation folded into the k=3
    # average would almost certainly perturb it away from an exact integer.
    assert result["rc"] == 0, f"19-chain fixture did not exit 0: {result!r}"
    assert result["procs_per_call"] == 19.0, (
        f"19-process nested /bin/sh chain measured procs_per_call="
        f"{result['procs_per_call']}, expected exactly 19.0 "
        f"(k={result['k']})"
    )


def test_python_to_git_oracle_count_is_exactly_two(git_binary):
    """DERIVED, not observed-then-blessed: `1 interpreter + 1 git` (2.0),
    against the Windows decomposition in the now-deleted
    `test_detect_staged_rollback_spawn_budget.py`
    (1 interpreter + 1 git + 1 conhost) minus the conhost term, which has
    no Darwin analogue -- `_darwin_one_invocation` performs no console
    suppression step to allocate one for. `sys.executable` and an
    explicit, PATH-resolved git binary are used rather than a bare
    `python3`/`git`: an xcode-select `python3` stub or a Homebrew-vs-
    system git mismatch would inflate this count for a reason unrelated
    to the instrument under test."""
    _require_darwin()
    cmd = [
        sys.executable,
        "-c",
        f"import subprocess; subprocess.run(['{git_binary}', '--version'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
    ]
    result = batched_process_time_ms(cmd, k=3)
    # rc covers only the final of the k=3 invocations (primitive contract);
    # the exact `== 2.0` equality below carries the rest (Finding 1,
    # S3-tests review).
    assert result["rc"] == 0, f"python->git oracle fixture did not exit 0: {result!r}"
    assert result["procs_per_call"] == 2.0, (
        f"python->git oracle measured procs_per_call={result['procs_per_call']}, "
        f"expected exactly 2.0 (1 interpreter + 1 git; k={result['k']})"
    )


def test_immediate_reap_adversary_count_is_exactly_five_hundred_one():
    """The undercount-channel adversary named in the spike verdict
    (docs/research/spike-verdicts/2026-08-22-posix-spawn-count-for-the-
    brightline-instrument.md): a root that forks and immediately
    `waitpid`s a child, 500 times in a tight loop, so the child is very
    often already dead (a zombie, or fully reaped) by the time the
    `NOTE_FORK` event is processed and `proc_listchildpids()` runs.
    `proc_listchildpids()` lists zombies, so a reaped-but-not-yet-collected
    child is still enumerable at the moment it forked; the verdict ran
    this exact fixture three times and measured 501.000 every time.
    Expected count is DERIVED, not observed-then-blessed: 1 (the root
    interpreter itself) + 500 (one child per loop iteration) = 501.
    k=1, not k=3 like the triangulation fixtures above -- the loop body
    already spawns 500 real processes per invocation, and k=3 here would
    add another thousand for no additional evidence (CLAUDE.md load-norm
    directive against careless process-time cost on a box carrying 50-70
    concurrent sessions).

    THE REAL CONTRACT (AC7, post-fix): this fixture drives a measured
    ~20% attach-failure rate (4/20 runs, n=20, on this box) even after the
    bounded kevent retry -- `_darwin_one_invocation`'s attach step and this
    adversary's own reap-before-register race are both real, so a run
    landing in that residual is expected, not a flake to chase away. The
    primitive's actual contract is: return EXACTLY 501, or RAISE
    `RuntimeError` -- it must never return any OTHER count (the exact
    silent-undercount failure this whole plan exists to prevent). A raise
    here is therefore a PASS of that contract, not a skip and not a
    failure -- this test does not retry-until-green to hide the rate."""
    _require_darwin()
    reap_loop = (
        "import os\n"
        "for _ in range(500):\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n"
    )
    cmd = [sys.executable, "-c", reap_loop]
    try:
        result = batched_process_time_ms(cmd, k=1)
    except RuntimeError as exc:
        assert "attach_failed" in str(exc), (
            f"batched_process_time_ms raised RuntimeError for a reason other "
            f"than the attach_failed residual this adversary targets: {exc!r}"
        )
        return
    assert result["rc"] == 0, f"500-iteration reap adversary did not exit 0: {result!r}"
    assert result["procs_per_call"] == 501.0, (
        f"500-iteration immediate-reap adversary measured procs_per_call="
        f"{result['procs_per_call']}, expected exactly 501.0 "
        f"(1 root + 500 forked-and-reaped children; k={result['k']}) -- "
        "the primitive must return exactly 501 or raise, never any other count"
    )


def test_deep_chain_count_is_exactly_prime_eleven(chain_script):
    """The third, independent triangulation leg -- also prime (11), so it
    cannot alias with a doubling/halving defect against itself OR against
    the depth-19 fixture above."""
    _require_darwin()
    result = batched_process_time_ms(["/bin/sh", chain_script, "11"], k=3)
    # rc covers only the final of the k=3 invocations (primitive contract);
    # the exact `== 11.0` equality below carries the rest (Finding 1,
    # S3-tests review).
    assert result["rc"] == 0, f"11-chain fixture did not exit 0: {result!r}"
    assert result["procs_per_call"] == 11.0, (
        f"11-process deep chain measured procs_per_call="
        f"{result['procs_per_call']}, expected exactly 11.0 (k={result['k']})"
    )


# ---------------------------------------------------------------------------
# AC6 -- the ESRCH seam. NOTE_TRACK is off-limits (C1); a reaped/nonexistent
# pid drives the same registration-rejection path.
# ---------------------------------------------------------------------------


def test_registering_a_reaped_pid_raises_rather_than_reporting_one_process(monkeypatch):
    """A pid this test itself spawned and already reaped via `os.waitpid`
    is genuinely, unambiguously dead before `_darwin_one_invocation` ever
    tries to register a kevent on it -- `_posix_spawnp_suspended` is
    monkeypatched to hand that pid back as the "freshly spawned" root, so
    kevent registration hits ESRCH for real. The observable failure
    (verified live during this file's construction) is a raise --
    concretely `ProcessLookupError` from the subsequent `os.kill` on the
    already-reaped pid -- never a silent `procs_per_call == 1`."""
    _require_darwin()
    # Darwin-only site (_require_darwin() above; os.waitpid is POSIX-only), so
    # the splat is inert AT RUNTIME -- review: coordinatorcode-reviewer
    # .ad915a07f1fc080c3 Finding 4, declined. It is not inert to the STANDING
    # GATE: `test_no_bare_test_tree_spawn` walks the test tree by AST and knows
    # nothing about platform guards, so deleting the splat reads as a bare
    # spawn and trips it. Teaching that gate to evaluate `_require_darwin()`
    # reachability, for one site, costs more than a no-op mapping does.
    proc = subprocess.Popen(["/bin/sh", "-c", "true"], **no_console_passthrough_kwargs())
    reaped_pid = proc.pid
    os.waitpid(reaped_pid, 0)

    monkeypatch.setattr(process_time, "_posix_spawnp_suspended", lambda argv, env: reaped_pid)

    with pytest.raises(OSError):
        process_time._darwin_one_invocation(["/bin/sh", "-c", "true"], None, None)


# ---------------------------------------------------------------------------
# AC4 -- process-time is correctly SCOPED: unrelated concurrent child CPU,
# reaped by a different thread, must not move the measured figure.
# ---------------------------------------------------------------------------


def _spawn_detached_noise_and_reap_first_child(cpu_ms: int) -> None:
    """Double-forks a noise process that burns roughly `cpu_ms` of CPU,
    detached from the measuring process's own child list. The immediate
    child forks once more then exits immediately (reaped synchronously
    here, before this function returns), orphaning the grandchild -- which
    `setsid()`s and execs the actual busy loop, running concurrently with
    whatever measurement the caller performs next, entirely outside this
    process's `proc_listchildpids(os.getpid())` view. This is what lets
    the noise be genuine and concurrent without tripping
    `_darwin_one_invocation`'s own pre/post ambient-children purity
    assertion (see module docstring above for why a same-process,
    different-thread `Popen.wait()` does not work as a seam here)."""
    busy_loop = (
        "import time; t0 = time.process_time();\n"
        f"while (time.process_time() - t0) * 1000.0 < {cpu_ms}: pass"
    )
    pid = os.fork()
    if pid == 0:
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        os.execvp(sys.executable, [sys.executable, "-c", busy_loop])
    else:
        os.waitpid(pid, 0)


def test_measurement_is_scoped_and_unmoved_by_concurrent_unrelated_child_cpu():
    """Interleaved control/treatment, not a bare before/after (load
    variance note, dispatch brief): each of `n` rounds measures the light
    fixture once with no noise (control) and once with ~200ms of unrelated,
    detached, concurrent CPU running on the box (treatment), and the two
    medians are compared with an explicit tolerance. This asserts the
    NUMBER stays put under contamination pressure -- not that some guard
    fired, which a guard that fires on nothing would also satisfy."""
    _require_darwin()
    light_cmd = [sys.executable, "-c", "pass"]
    n = 5
    tolerance_ms = 25.0

    control = []
    treatment = []
    # Both k=3 probes below: rc covers only the final invocation (primitive
    # contract, Finding 1, S3-tests review) -- this test's actual claim is
    # carried by the control/treatment median comparison, not by rc.
    for _ in range(n):
        control_result = batched_process_time_ms(light_cmd, k=3)
        assert control_result["rc"] == 0
        control.append(control_result["process_time_ms"])

        _spawn_detached_noise_and_reap_first_child(cpu_ms=200)
        treatment_result = batched_process_time_ms(light_cmd, k=3)
        assert treatment_result["rc"] == 0
        treatment.append(treatment_result["process_time_ms"])

    control_median = statistics.median(control)
    treatment_median = statistics.median(treatment)
    assert treatment_median <= control_median + tolerance_ms, (
        f"process_time_ms moved from a control median of {control_median}ms "
        f"to a treatment median of {treatment_median}ms (n={n}) under "
        "200ms of concurrent, other-thread-reaped, unrelated child CPU -- "
        "this is exactly the getrusage(RUSAGE_CHILDREN) process-wide "
        "contamination trap C1's wait4-scoped fix exists to close"
    )


# ---------------------------------------------------------------------------
# AC18 -- the instrument's own overhead, priced against the same brightline
# it gates.
# ---------------------------------------------------------------------------


def test_instrument_overhead_is_priced_against_the_brightline():
    """An instrument that costs more than the thing it prices is a defect
    and nothing else in this file prices it. A single k=1 call against the
    full 500ms brightline cannot fail short of catastrophe (Finding 2,
    S3-tests review) -- a minimal call is on the order of tens of ms, so a
    bare `<= 500.0ms` assertion discriminates nothing. Instead this prices
    the instrument as a FRACTION of the brightline (10%, i.e. 50ms) over
    `n=9` samples' p90, so the assertion can actually fail if the
    instrument becomes meaningfully expensive without requiring a
    catastrophic 10x regression to trip it. `n=9` keeps the added cost
    modest -- this box runs 50-70 concurrent LLM sessions (CLAUDE.md load
    norm) -- while still giving a p90 (the 8th of 9 sorted samples) some
    resistance to a single noisy sample."""
    _require_darwin()
    overhead_fraction_ms = DR344_BRIGHTLINE_MS * 0.10
    n = 9
    samples_ms = []
    for _ in range(n):
        t0 = process_time.time.perf_counter()
        result = batched_process_time_ms([sys.executable, "-c", "pass"], k=1)
        samples_ms.append((process_time.time.perf_counter() - t0) * 1000.0)
        assert result["rc"] == 0
    samples_ms.sort()
    p90_index = min(int(round(0.9 * (n - 1))), n - 1)
    p90_ms = samples_ms[p90_index]
    assert p90_ms <= overhead_fraction_ms, (
        f"batched_process_time_ms's own p90 wall cost over n={n} samples "
        f"was {p90_ms}ms (samples: {samples_ms}), exceeding "
        f"{overhead_fraction_ms}ms -- 10% of DR-344's {DR344_BRIGHTLINE_MS}ms "
        "brightline -- the instrument itself is getting expensive relative "
        "to what it gates"
    )
