"""
coordinator_core.benchmarks.tests.test_commit_op_wallclock_budget

C2 of docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md — "Give
the op a real wallclock and spawn baseline through the warm transport."

WHAT THIS FILE WAS, AND WHAT C6 CHANGES. This chunk was originally
NOT-contingent instrument-only (C2's own body: "it cannot invalidate what
follows") — C1 wired `ceremony.commit` to `run_commit_pipeline` UNCHANGED,
so every sample here paid the pipeline's two-git-invocation agree branch and
was expected to be BAD by construction. C3 (in-process object write) and C5
(safe-commit routing) have since landed. C6
(docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md) is the chunk
that reads these numbers against the plan's own AC4/AC7 targets and closes
them — so the two numeric gates below (AC4's 150ms wallclock median, AC7's
"AC4's target holds under concurrent load") are now REAL assertions, not
recorded-only instrument sanity.

STALE-NUMBER WARNING, 2026-08-26 (session a2d4a470). An earlier MEASURED
block here recorded 551.6ms wallclock / 849.4ms p95 / 695.3ms process time /
35.75 job-object processes/call / 3959.1ms 8-way p50 -- COLD-path numbers
under a warm label, predating `a28dd424`. Do not cite those figures; they
are removed from this docstring rather than left for a reader to stumble
into. Superseded numbers, measured through one identical shape per rung
(docs/research/spike-verdicts/2026-08-26-where-the-commit-op-s-other-half-
second-goes.md): wallclock median 360.4ms, in-process handler 371.4ms over
**11 git spawns** (320.0ms, 86.1%), 8-way p50 1232.8ms with 0/8
indeterminate.

INSTRUMENT FIX, C5 of docs/plans/2026-08-26-the-commit-op-stops-asking-git-
eleven-times.md. Two defects in THIS file's prior shape, both now fixed:
`_write_driver` emitted a driver that ITSELF spawned the invoke door, so
every wallclock/concurrent sample carried TWO interpreter starts (~37ms of
pure floor) while AC5's dial leg is measured at ONE — the two numbers were
never comparable. The wallclock and concurrent legs now call
`_invoke_commit_argv` directly (`_prepare_commit_invocation` mutates
`tracked.txt` and writes the params file OUTSIDE the timed window, in the
harness loop, before `time.perf_counter()` starts) — ONE interpreter per
sample, the same shape AC5's dial leg measures. The process-time/spawn-count
leg still uses `_write_driver` + `batched_process_time_ms`, which by
construction re-runs identical argv and so cannot vary content from outside;
that leg's process-time figure therefore still folds in the driver's own
interpreter start (module's own DRIVER SHAPE section below), and the
job-object process count under-reports (3.0 vs the census's 11) because it
sees landing spawns, not the gate path — a separate, already-known
limitation of that leg, not something this fix claims to close.

MEASURED 2026-08-26/27 (C6, this chunk, against the rebuilt post-C5
instrument, isolated warm server, this live tree, two consecutive runs).
AC4 FAILS: wallclock median=204.687ms p50=204.687ms p95=391.828ms
min=159.567ms max=991.894ms (n=15) against the 150.0ms target -- a
54.687ms miss, process_time=105.469ms procs_per_call=3.0 (k=8,
driver-inclusive leg, see DRIVER SHAPE above). AC7 FAILS far more severely
under 8-way distinct-worktree concurrent load, and on its own added
criterion, not only its wallclock target: run 1 measured p50=2070.839ms
p95=2083.605ms with 0/8 -32004 responses; a second run of the identical
instrument measured p50=2347.585ms p95=2571.324ms with 1/8 calls returning
-32004 WARM_DISPATCH_INDETERMINATE ("no response within 2.0s") -- both
runs cluster in the ~2.0-2.6s band, both far over AC4's own 150.0ms target,
and the second run additionally fails the criterion this chunk adds in its
own right (0 -32004 responses). That band sits almost exactly on the 2.0s
dispatch-deadline figure named elsewhere in this repo's doctrine, which is
evidence (not proof -- this instrument does not itself attribute cause)
that AC7's degradation is a deadline/retry artifact of concurrent dispatch
rather than a linear scaling of AC4's own per-call cost, and that the
-32004 criterion is not a rare edge case but reachable on ordinary runs of
this exact load shape.

C1 DOOR FIX (docs/plans/2026-08-27-the-stopwatch-is-fixed-then-the-index-re-
reads-stop.md). The wallclock and concurrent legs' `_invoke_commit_argv`
previously built `python -m coordinator_core.invoke ceremony.commit ...` --
real callers dial `coordinator-invoke.exe`, never that Python module form.
The delta is a measured CONSTANT of ~51-56ms across every cell of the spike
this chunk cites (ping 22.9 vs 78.7, commit 63.0 vs 113.7, 8-way 25.3 vs
76.6ms) -- larger than AC4's own reported 39.4ms miss on an earlier
measurement round. `_invoke_commit_argv` now builds argv against the door
binary hardlinked into the isolated engine root
(`coordinator_core/warm/door/door.exe`), pointed at that same isolated root
via `COORDINATOR_DOOR_ENGINE_ROOT` (`door.c :: resolve_engine_root`'s own
documented override -- the same mechanism `test_door_read_deadline.py ::
_run_door` uses). The code under measurement stays THIS tree's: the door is
only a native JSON-RPC framer onto the isolated pipe, never a second engine.
The MEASURED figures recorded below predate this fix and were captured
through the Python-module shape -- read them as the prior instrument's
numbers, superseded by whatever this fixed instrument next records, not
re-validated against the door.

NEITHER GATE CLOSES. Per this chunk's own brief ("If a number misses, the
chunk says so and the plan does not close — the answer is a cheaper op,
never a wider AC"), AC4_TARGET_MS stays 150.0 unmoved and neither assertion
above is loosened to match the measured figures. AC5's dial leg alone (a
zero-git `ping` through the identical door) measures ~82-105ms process time
(procs_per_call=3.0 in this run's driver-inclusive leg), so the transport
itself is not the residual cost: the excess named by the spike sits inside
the commit op's own handler path (the four commit-leg gates, trailer
assembly, and/or hooks still walking `subprocess.run` rather than the
in-process object-write machinery C3 shipped for the commit itself) — a
design/mechanism gap in files this chunk's `writes:` scope excludes
(`commit_op.py`, `git_native.py`, `commit_pipeline.py`), not a measurement
artifact of this instrument. AC7's own 2.0-2.6s-band clustering, and its
one observed -32004, additionally implicate a deadline/retry mechanism
reachable through concurrent dispatch, itself outside this chunk's
`writes:` scope to name precisely — recorded here as a MEASURED finding
for the plan's own next chunk to read, not remediated by this one.

THREE COLUMNS, NEVER COLLAPSED (plan task body, verbatim instruction).
wallclock (median/p50/p95), process time, and job-object spawn count are
three separate measurements answering three separate questions —
`coordinator_core.benchmarks.process_time` module docstring: process time
and spawn count read peer-load-free cost; wallclock is the only axis that
sees ENGINE QUEUEING, which is invisible to both of the others (a resident
server serialising concurrent commits reaches its own door in 3.9ms but can
then wait behind peers). No number in this file substitutes for another.

INSTRUMENT (C1): `coordinator-invoke.exe ceremony.commit <params> --repo
<path>` — the real native door binary (`coordinator_core/warm/door/door.c`),
hardlinked into the isolated engine root and pointed at it via
`COORDINATOR_DOOR_ENGINE_ROOT`, never a direct in-process call to
`run_commit_pipeline` or the op handler and never the `python -m
coordinator_core.invoke` module form this file used before C1 (see C1 DOOR
FIX above) — AC4 says "warm-served", and only the door actually attempts a
warm dial before falling back cold, at the process-start cost real callers
actually pay.

WARM SERVER: this live tree carries no `coordinator_core/_engine_stamp`
(DR-315 §2 — an unstamped dev checkout is not a warm-server HOST, so every
call FROM it goes cold unconditionally, per `test_op_cli_warm_hop_process_
time.py`'s own C4 finding). `warm_engine_root` below duplicates
`test_warm_door_process_time_gate.py::warm_engine_root`'s own isolation
recipe (hardlink `coordinator_core/` into a fresh temp dir, boot `python -m
coordinator_core.warm.server` against it, poll for a PID-alive breadcrumb)
rather than importing that fixture — a pytest fixture function cannot be
called directly outside pytest's own fixture protocol (this pytest version
raises "Fixture ... called directly"), the same constraint that module's
own `_short_runtime_base` docstring records for its own DELIBERATE
DUPLICATE. ONE DELIBERATE DIVERGENCE from that recipe: this file hardlinks
`coordinator_core/` from THIS LIVE DEV TREE, never a published sibling
clone, and stamps the isolated destination itself (`is_engine_root`'s own
contract — `engine_root.py`: "only its BYTES matter... readable and
non-empty", no content validation) rather than requiring the SOURCE to
already carry one. `ceremony.commit` (this plan's own C1) exists only in
this dev tree's uncommitted/unpublished work — the published sibling mirror
`test_warm_door_process_time_gate.py` hardlinks FROM does not carry it yet
(confirmed empirically: dispatching against a hardlink of that mirror
returns `-32601 Method not found: 'ceremony.commit'`), so measuring THIS
op through a real warm server requires serving THIS tree's own bytes.
Skips (never fails) when this file's own `coordinator_core/` package is
absent, which cannot happen in a checked-out repo but is named for parity
with the sibling gate's skip discipline.

TWO DIFFERENT SHAPES, POST-C1/C5. The wallclock leg (AC4) and the concurrent
leg (AC7) time the door's own argv DIRECTLY via `_invoke_commit_argv` — no
driver, ONE process start per sample, `coordinator-invoke.exe` itself, not a
Python interpreter — with `_prepare_commit_invocation`
mutating `tracked.txt` and writing the `--params-file` sidecar OUTSIDE the
timed window, in the harness loop, before the clock starts. The
process-time/spawn-count leg (still recorded, never gated) keeps the driver
shape: `batched_process_time_ms` re-runs the SAME argv `k` times, so
per-invocation commit content must differ from OUTSIDE that call, which only
a driver can do -- the driver mutates `tracked.txt` to fresh content
IN-PROCESS, then spawns `python -m coordinator_core.invoke ceremony.commit
... --params-file <path>` as a CHILD and exits with its own return code,
forwarding stdout so this file's callers can still read the JSON-RPC
envelope. `--params-file` (not the positional `params_json` argv form) is
deliberate, not incidental: the params carry a JSON object with
braces/spaces (`invoke/__main__.py`'s own docstring names exactly this
payload shape as `--params-file`'s reason to exist -- quoting-immune,
ARG_MAX-safe). The driver's own interpreter start is therefore still counted
alongside the invoke door's in that one leg's process-time figure; unlike
`test_commit_path_process_budget.py`'s driver-residue calibration, this file
does not subtract it out for that leg -- it is a known, named limitation of
the process-time/spawn-count column only, never of the wallclock or
concurrent columns AC4/AC7 gate.

AC5's DIAL LEG: measured as `ping` (a "none"-scoped, zero-git, near-instant
op) against the SAME isolated warm server — the cost of reaching the engine
at all, reported separately from AC4's whole-commit total, per this file's
task body ("Reach to the engine alone... reported separately from AC4's
total").

AC7's CONCURRENT ARM: >=8 simultaneous commits from INDEPENDENT repos (each
its own `git init`, own object store, own refs — never `git worktree add`
off one shared base repo), all dialing the SAME isolated warm server — the
shape that exposes ENGINE QUEUEING (a resident server serialising
concurrent commits).

C3 WORKTREE FIX (docs/plans/2026-08-27-the-stopwatch-is-fixed-then-the-
index-re-reads-stop.md). This arm previously built its N_CONCURRENT callers
as `git worktree add` off ONE shared base repo, and its own docstring
claimed this made engine queueing "the term under measurement" because
distinct worktrees do not share an `index.lock`. True, and beside the
point: worktrees off one base share the OBJECT STORE and refs, and at k=1
with no concurrency in play at all that shared-store shape measured
130.9ms against 63.0ms for an ordinary independent repo — the rig
introduced a term roughly twice the size of the one it set out to isolate,
confounding engine queueing with shared-object-store contention. The fleet
does not commit from worktrees anyway (CLAUDE.md: parallel agents share one
tree, separated by disjoint file scope, never by checkout), so the shape
was unrepresentative as well as confounded. Each of the N_CONCURRENT
callers now gets its own independently-`git init`'d repo
(`_build_fixture_repo`, same recipe the wallclock leg uses), sharing
nothing but the warm server under test.

FIXTURE-SIZE PARAMETERISATION (C4, docs/plans/2026-08-27-the-stopwatch-is-
fixed-then-the-index-re-reads-stop.md). Every fixture repo here previously
carried a ONE-ENTRY index, while claude-klabauter's own working tree carries 37,334 --
the op's dominant cost (index read/write) is invisible at n=1 BY
CONSTRUCTION, the same genus of defect as the LF-only-fixture failure this
plan's C1/C2 already fixed on a different axis (`state/lessons/2026-08-26-a-
fixture-is-a-claim-about-the-world.yaml`). `_build_fixture_repo` now takes an
optional `base_repo`: `None` keeps the original ~1-entry `git init` shape,
used for the small-index case that stays a "does the instrument still work
at all" contrast; a real `base_repo` clones `--local` from a base built ONCE
by the module-scoped `large_index_base_repo` fixture (>=30,000 tracked
entries) rather than re-populating 30,000 files per repo instance -- measured
353s to build eight such repos via `git add` against 63s via `git clone
--local` off one built-once base (dispatch brief). `--local` hardlinks the
object store into each clone; safe here because objects are immutable and
this plan's own C3 measured defect was the shared `refs/index.lock`, never
shared blobs (staff-eng review finding 8). The two GATED wallclock/concurrent
tests are now parameterised over `index_size` (`1` and `30_000`); the large
case is expected to go RED against AC4/AC7's existing 150.0ms target -- that
is this chunk's own correct, intended outcome, not a reason to widen the
target or soften the assertion.

Spec backlink: docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md,
C2. AC4/AC5/AC7 are read against this instrument by a later chunk (C6); this
file discharges C2's own task body only.

C9 VERDICT (2026-08-27, session 0eeb902a). `test_c9_route_table_verdict_per_
arm` adds the per-arm (gate-open vs gate-closed) route-table report this
plan's C9 chunk is the deliverable of. MEASURED RESULT: the instrument
cannot currently produce ANY door-measured figure. EVERY sample on BOTH
arms, at index_size=1, returned WARM_DISPATCH_INDETERMINATE (-32004) --
including a bare `ping` through `door.exe` with no git work at all -- while
the byte-identical request via `python -m coordinator_core.invoke` against
the SAME isolated server succeeds every time (verified by hand: 6/6 door
`ceremony.commit` calls -32004, 1/1 door `ping` -32004, 1/1 python-module
`ceremony.commit` rc=0 committed:true, immediately before and after). This
is NOT the 2.0s-deadline mechanism the predecessor plan's AC7 finding
named -- door samples return in ~10-30ms, an order of magnitude under that
deadline -- and it reproduces on the FIRST invocation against a freshly
booted isolated server, not only under concurrent load. It is a
door-binary-vs-python-client discrepancy in THIS isolated-clone recipe
(`warm_engine_root`), scoped to `coordinator_core/warm/door/door.c` and
`door_core.c` -- files outside this chunk's `writes:` scope to fix, and
undiagnosed past "the server returns some error code the door's own
`is_provably_undispatched` whitelist does not cover, so it refuses to fall
through" (`door_core.h`'s own documented classification). AC1's structural
argv assertion (`test_invoke_commit_argv_uses_the_door_binary_not_the_
python_module`) still passes -- the door IS being dialed correctly at the
argv level; the failure is what the SERVER sends back once dialed. The
30,000-entry leg was not run: the failure reproduces on the FIRST bare
`ping`, before any index-size-dependent work, so building that fixture only
to hit the identical door-level failure was not worth its own build cost.
NEITHER AC4 NOR AC7 (the predecessor plan's re-measurement, C9's own body)
could be re-measured for the same reason -- both were ALREADY red before
this chunk, for this identical root cause (confirmed: the pre-C9 tree's own
`test_commit_op_wallclock_and_spawn_baseline_are_recorded[index-1]` fails
the same way). R1-R3 stay unverified at this door (this plan's scope
excludes the op-side files, unchanged by the rescope). R4 stays CLOSED
(priced once-per-process, ~0% warm, not re-measured per commit). R5 stays
UNVERIFIED AT THIS DOOR: `procs_per_call=3.0` (k=8, driver-inclusive leg,
this leg's own known limitation -- module docstring's DRIVER SHAPE note),
neither confirming nor refuting the confirmed-cold `conhost.exe` finding.
**The headline: this plan's own AC10 (distance to 78.1ms, per arm) cannot
be reported on this box today** -- not "the op misses the floor" but "the
instrument that would measure the miss cannot complete a single call",
which is the failure mode one door further than every prior finding on
this chain. Left red per this chunk's own brief ("If it misses, say so with
the number and leave it red"); no number exists to report because the
door itself does not return one.

CONCURRENT-DRAIN FIX (C2, docs/plans/2026-08-27-the-stopwatch-is-fixed-then-
the-index-re-reads-stop.md). AC7's concurrent leg previously drained procs
via `proc.communicate()` in a plain launch-order loop -- sample i's own
elapsed was `time.perf_counter() - start[i]` computed only AFTER draining
proc i, which itself only runs after every proc 0..i-1 has already been
drained. A slow earlier proc therefore raises every later sample's measured
elapsed to at least its own finish time, manufacturing a tightly-clustered
floor that reads as engine queueing but is actually "when did the harness
get around to reading you". Measured on identical procs: true p50 1610.2ms
against the prior formula's 1793.0ms. `_drain_concurrent_samples` below
gives each proc its OWN waiter thread, stamping that proc's completion the
moment ITS `communicate()` returns -- no proc's sample can be inflated by
any other proc's drain order. `test_concurrent_drain_isolates_each_procs_
own_exit` pins this directly: a deliberately-slow first proc must not raise
a later, fast proc's measured sample.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    LiveTreeAccountant,
    batched_process_time_ms,
)
from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.warm import breadcrumb
from coordinator_core.session.core import stable_pid_alive

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_WALLCLOCK_SAMPLES = 15
"""Plan task body: "wallclock median, p50, p95 ... k>=15"."""

K_PROCESS_TIME_INVOCATIONS = 8
"""Amortisation factor for the process-time/spawn-count leg — matches the
plan's own C3/C4 methodology (`k>=8`) elsewhere in this package."""

N_CONCURRENT = 8
"""AC7: "at least 8 simultaneous commits" -- from independent repos, not
worktrees off a shared base (module docstring's C3 WORKTREE FIX)."""

LARGE_INDEX_ENTRY_COUNT = 30_000
"""C4 dispatch brief: ">=30,000-entry case" -- claude-klabauter's own working tree
carries 37,334 tracked entries; this stays comfortably in that regime
without paying that exact figure's build cost."""

INDEX_SIZES = [1, LARGE_INDEX_ENTRY_COUNT]
"""C4: "keep a ~1-entry case for contrast and add a >=30,000-entry case
that the GATED legs run against." `1` reuses the original `git init`
shape (`base_repo=None`); `LARGE_INDEX_ENTRY_COUNT` clones `--local` off
`large_index_base_repo`."""

INDEX_SIZE_IDS = ["index-1", f"index-{LARGE_INDEX_ENTRY_COUNT}"]

AC4_TARGET_MS = 150.0
"""Plan's own AC4 row: "Warm-served, the op commits in <=150ms wallclock
measured end-to-end from the caller." AC7 reuses this same figure -- its
own row is "AC4's wallclock holds ... under concurrent load", not a
distinct number."""

_ENGINE_ROOT_OVERRIDE_ENV = "COORDINATOR_WARM_GATE_ENGINE_ROOT"
_BOOT_WAIT_DEADLINE_SECS = 20.0
_BOOT_POLL_INTERVAL_SECS = 0.25
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_TIMEOUT_S = 60


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip(
            "process-time job-object accounting and this file's isolated-server "
            "boot recipe are Windows-only in this file (mirrors test_warm_door_"
            "process_time_gate.py's own Windows fixture); no Darwin/Linux leg "
            "is authored here"
        )


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
        creationflags=_CREATE_NO_WINDOW,
    )


def _env(**overrides) -> dict:
    base = dict(os.environ)
    base.setdefault(
        "COORDINATOR_ENGINE_ROOT", str(Path(__file__).resolve().parents[2].parent)
    )
    # `warm/settings.py::is_warm_enabled()` resolves COORDINATOR_WARM, then
    # the `engine.warm.enabled` machine-registry rung, then falls through to
    # off. The isolated `warm_engine_root` clone hardlinks `coordinator_core/`
    # only -- none of the machine registry that second rung reads -- so a
    # driver relying on the registry rung to reach warmth goes cold
    # unconditionally inside this fixture, regardless of the box's own
    # machine-wide setting. Setting the env rung here is therefore necessary
    # for these drivers to ever dial the isolated server at all. It is
    # deliberately NOT sufficient on its own: requesting warmth only fixes
    # today's stripped-registry input, and warm-off is itself a legitimate
    # supported configuration under which a cold dispatch is
    # indistinguishable from a correctly-configured one by inspecting env
    # alone -- see `_assert_warmth_or_fail` below, which asserts the outcome
    # from the transport's own warm-vs-cold attribution rather than trusting
    # this request to have landed.
    base.setdefault("COORDINATOR_WARM", "1")
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Isolated warm server — deliberate duplicate of test_warm_door_process_time_
# gate.py::warm_engine_root's own recipe (see module docstring: a pytest
# fixture cannot be called directly outside pytest's own protocol).
# ---------------------------------------------------------------------------


def _source_root() -> Path:
    """The tree to hardlink `coordinator_core/` FROM. An explicit override
    always wins (a different box's clone layout); otherwise THIS LIVE DEV
    TREE (module docstring's ONE DELIBERATE DIVERGENCE from the sibling
    gate's own recipe) — `ceremony.commit` exists only here, not on any
    published mirror this box may also carry."""
    override = os.environ.get(_ENGINE_ROOT_OVERRIDE_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3]


def _write_isolated_stamp(isolated_root: Path) -> None:
    """Manufactures a valid `_engine_stamp` in the ISOLATED destination
    only, never the source — `is_engine_root`'s own contract (module
    docstring) validates only readability/non-emptiness, not content, so
    this is a legitimate stamp, not a spoof of one."""
    stamp_path = isolated_root / "coordinator_core" / "_engine_stamp"
    stamp_path.write_text(f"sha:commit-op-wallclock-{uuid.uuid4().hex}\n", encoding="utf-8")


def _hardlink_coordinator_core(source_root: Path, isolated_root: Path) -> int:
    n = 0
    src_pkg = source_root / "coordinator_core"
    for root, dirs, files in os.walk(src_pkg):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(src_pkg)
        dst_dir = isolated_root / "coordinator_core" / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, dst_dir / name)
            n += 1
    return n


def _wait_for_live_breadcrumb(isolated_root: Path, deadline_secs: float) -> Optional[dict]:
    deadline = time.time() + deadline_secs
    while time.time() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb:
            pid = crumb.get("pid")
            epoch = crumb.get("stable_pid_start_epoch") or ""
            if pid is not None and stable_pid_alive(pid, stored_start_epoch=str(epoch)):
                return crumb
        time.sleep(_BOOT_POLL_INTERVAL_SECS)
    return None


def _assert_warmth_or_fail(isolated_root: Path) -> None:
    """Warmth PRECONDITION for this whole module -- assert it, do not merely
    request it (dispatch brief). `_env()` setting `COORDINATOR_WARM=1` makes
    a warm dial POSSIBLE; it cannot make it true, because warm-off is a
    legitimate supported configuration and a cold dispatch under it is
    indistinguishable from a correctly-configured one by inspecting request
    env alone. The instrument does not malfunction in that case -- it
    faithfully measures a real (cold) mode and, absent this check, reports it
    under a label ("warm-served") that requesting warmth implied but never
    established.

    Reuses `coordinator_core.warm.telemetry.client_cold_count` -- the
    module's OWN warm-vs-cold attribution primitive
    (`warm/client.py::_record_cold_fallback` is the sole call site able to
    observe a cold fallback, module docstring: "THE CLIENT IS THE ONLY
    PROCESS THAT CAN OBSERVE A COLD FALLBACK") -- rather than inventing a new
    marker. One cheap `ping` (a COMPUTE_ONLY, zero-git op) is dialed through
    the exact same door (`python -m coordinator_core.invoke`) and env
    (`_env()`) the measurement drivers use; `client_cold_count` is read
    before and after. No increment means this process's one dispatch reached
    a running warm server and was served by it -- an increment, or a
    non-zero return code, means it fell back cold, and every figure this
    module would otherwise record under a "warm-served" label would
    actually be a cold one wearing that label.
    """
    from coordinator_core.warm import telemetry

    env = _env()
    # A few retries, not a loosening of the assertion: the breadcrumb this
    # fixture already waited on proves the server PROCESS is alive, not that
    # its op registry has finished preloading (`_preload_op_registry` runs
    # AFTER election, ~703ms of imports on the first dispatch --
    # `warm/telemetry.py::record_server_boot`'s own docstring) or that its
    # single pending listener (module docstring's "the warm server keeps a
    # single pending listener") has reached accept for this probe. A dial
    # that races that window is not the "warm-off" case this precondition
    # exists to catch -- it is retried; a dial that keeps missing is not.
    attempts = 3
    completed = None
    before = after = 0
    for attempt in range(attempts):
        before = telemetry.client_cold_count(engine_root=isolated_root)
        completed = subprocess.run(
            [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
            cwd=str(isolated_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        after = telemetry.client_cold_count(engine_root=isolated_root)
        if completed.returncode == 0 and after == before:
            return
        if attempt < attempts - 1:
            time.sleep(_BOOT_POLL_INTERVAL_SECS * 2)
    if completed is None or completed.returncode != 0 or after > before:
        pytest.fail(
            "warmth precondition FAILED: a probe 'ping' dispatched through the "
            "same invoke door and env the measurement drivers use was not "
            f"confirmed warm-served (rc={completed.returncode}, "
            f"client_cold_count(isolated_root) before={before} after={after} "
            "-- an increment means warm/client.py::try_warm_dispatch fell "
            "back cold for this probe). Every figure this module records is "
            "gated on this check because warm-off is a legitimate "
            "configuration indistinguishable from a cold dispatch by env "
            f"alone. stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )


def _terminate(pid: int) -> None:
    import psutil

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass


@pytest.fixture(scope="module")
def warm_engine_root() -> Iterator[Path]:
    _require_windows()

    source_root = _source_root()
    if not (source_root / "coordinator_core").is_dir():
        pytest.skip(
            f"{source_root!r} carries no coordinator_core/ package to hardlink -- "
            f"point {_ENGINE_ROOT_OVERRIDE_ENV} at a real checkout to run this file"
        )

    tmp_parent = mkdtemp_for_clone(source_root, prefix="commit-op-wallclock-")
    isolated_root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _hardlink_coordinator_core(source_root, isolated_root)
        _write_isolated_stamp(isolated_root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(isolated_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        crumb = _wait_for_live_breadcrumb(isolated_root, _BOOT_WAIT_DEADLINE_SECS)
        if crumb is None:
            if proc.poll() is None:
                proc.terminate()
            pytest.skip(
                f"isolated warm server (source={source_root}) did not reach a "
                f"PID-alive breadcrumb within {_BOOT_WAIT_DEADLINE_SECS}s"
            )
        _assert_warmth_or_fail(isolated_root)
        yield isolated_root
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb and crumb.get("pid") is not None:
            _terminate(int(crumb["pid"]))
        elif proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        # The breadcrumb pid is NOT the whole process set this fixture owns:
        # `server.py`'s boot spawns the http listener DETACHED via
        # `supervisor.ensure_listener`, so it survives the terminate above and
        # holds this clone open against the removal below. Reap by ROOT, the
        # one predicate a deliberately-reparented process still answers to.
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=isolated_root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="commit_op_engine_root", reaped=reaped)


@pytest.fixture(scope="module")
def server_accountant(warm_engine_root) -> Iterator[LiveTreeAccountant]:
    """Job-object accounting attached to the ISOLATED WARM SERVER, so a
    per-sample process-time and spawn-count delta is available on the one
    axis DR-344 and CLAUDE.md permit.

    Attached to the server rather than to the client because that is where
    the op actually runs: `coordinator-invoke.exe` is a JSON-RPC framer over
    a pipe, and every `git` child the op spawns -- plus the `conhost.exe`
    DR-373 measured beside each one -- is a child of the SERVER. A job around
    the client accounts for the framer and nothing the op did.

    Attachment happens after `warm_engine_root` has already asserted warmth,
    so the server's boot and `_preload_op_registry` import cost sit outside
    both ends of every delta and can never be amortised into a per-commit
    figure. The detached http listener `supervisor.ensure_listener` spawns at
    boot is likewise outside the job -- correct: it is a boot cost, not a
    per-op one.

    Skips rather than fails when the breadcrumb carries no pid: without one
    there is no process to attach to, and a run that silently reported
    `process_time_ms=None` under a process-time heading would be the same
    class of mislabelling this whole file exists to stop.
    """
    crumb = breadcrumb.read_breadcrumb(engine_root=warm_engine_root)
    pid = crumb.get("pid") if crumb else None
    if pid is None:
        pytest.skip(
            f"no warm-server pid in the breadcrumb under {warm_engine_root} -- "
            "nothing to attach job-object accounting to"
        )
    accountant = LiveTreeAccountant(int(pid))
    try:
        yield accountant
    finally:
        accountant.close()


# ---------------------------------------------------------------------------
# Fixture repo(s) — one tracked file per repo, a local bare origin (no
# network round trip), no hooks (DR-356: pre/post-commit hook cost is OUT of
# an op's own budget; this file measures the op, not the hook chain).
# ---------------------------------------------------------------------------


def _build_fixture_repo(
    tmp_path: Path, branch: str, base_repo: Optional[Path] = None
) -> Path:
    """Build one fixture repo, parameterised over index size (C4 dispatch
    brief). `base_repo=None` keeps the original ~1-entry shape: `git init`
    from scratch with one tracked file, seeded and committed here. A real
    `base_repo` instead `git clone --local`s off `large_index_base_repo`'s
    once-built >=30,000-entry base and checks out a fresh branch -- this is
    what turns 353s of per-repo `git add` (module docstring: measured
    building eight such repos that way) into 63s of cloning, both against
    identical fixture content. `--local` hardlinks the object store into
    each clone; safe here because objects are immutable and this plan's
    own C3 measured defect was the shared `refs/index.lock`, never shared
    blobs (staff-eng review finding 8, module docstring)."""
    repo = tmp_path / "repo"
    if base_repo is None:
        repo.mkdir()
        _git(repo, "init", "-q", "-b", branch)
        _git(repo, "config", "user.email", "c2-wallclock@example.com")
        _git(repo, "config", "user.name", "c2-wallclock")
        (repo / "tracked.txt").write_text("seed\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed commit")
        return repo

    _git(tmp_path, "clone", "--local", "-q", str(base_repo), str(repo))
    _git(repo, "checkout", "-q", "-b", branch)
    _git(repo, "config", "user.email", "c2-wallclock@example.com")
    _git(repo, "config", "user.name", "c2-wallclock")
    return repo


@pytest.fixture(scope="module")
def large_index_base_repo(tmp_path_factory) -> Path:
    """The >=30,000-entry base repo `_build_fixture_repo` clones `--local`
    from, built EXACTLY ONCE per module (C4 dispatch brief: "Build the
    large fixture once and `git clone --local` it per repo") rather than
    once per test/repo instance -- lazily instantiated (only tests that
    request `index_size > 1` pull this via `request.getfixturevalue`, never
    unconditionally as a bare parameter) so the ~1-entry contrast case never
    pays this fixture's build cost."""
    base = tmp_path_factory.mktemp("commit_wallclock_large_index_base") / "base"
    base.mkdir()
    _git(base, "init", "-q", "-b", "work/c4-large-index-base")
    _git(base, "config", "user.email", "c2-wallclock@example.com")
    _git(base, "config", "user.name", "c2-wallclock")
    (base / "tracked.txt").write_text("seed\n", encoding="utf-8")
    for i in range(LARGE_INDEX_ENTRY_COUNT):
        (base / f"idx_{i:06d}.txt").write_text(f"seed {i}\n", encoding="utf-8")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed commit with >=30000-entry index")
    return base


def _prepare_commit_invocation(repo: Path, counter_path: Path) -> Path:
    """Mutates `tracked.txt` to fresh content and writes the `--params-file`
    sidecar OUTSIDE any timed window -- content variation and the params IO
    are harness setup, not part of the measured invocation (module
    docstring: "Per-iteration content mutation must happen OUTSIDE the timed
    window ... vary it in the harness before starting the clock, exactly as
    the spike's own ladder does"). Same idempotent-fixture trap
    `test_commit_path_process_budget.py::_write_driver` names -- re-running
    identical content would make every re-commit a no-op. Returns the
    `--params-file` path the caller builds its argv from."""
    n = int(counter_path.read_text()) if counter_path.exists() else 0
    n += 1
    counter_path.write_text(str(n))
    (repo / "tracked.txt").write_text(f"harness rev {n}\n", encoding="utf-8")
    params = {
        "subject": f"c2 wallclock baseline commit {n}",
        "stage_paths": ["tracked.txt"],
        "caller_paths": ["tracked.txt"],
        "push_mode": "none",
    }
    params_path = counter_path.with_suffix(".params.json")
    params_path.write_text(json.dumps(params), encoding="utf-8")
    return params_path


_INSTALLED_DOOR_EXE = (
    Path(os.path.expanduser("~"))
    / ".coordinator-claude-settings"
    / "bin"
    / "coordinator-invoke.exe"
)
"""Resolved EXACTLY ONCE, at import time, into a module-level constant --
the same shape `coordinator_core/testing/symlink_capability.py` uses for its
capability probe, and for a harder reason here.

The suite root's `conftest.py` quarantines `HOME`/`USERPROFILE` into a
throwaway `home-quarantine*` dir for every unmarked test, so a `Path.home()`
call inside a fixture or test body resolves to that quarantine and this gate
skips itself with "no installed coordinator-invoke.exe" on a box that has
one. Module import runs at COLLECTION, before the fixture's monkeypatch, so
this constant sees the real profile. Measured: resolving at call time skipped
2/2 C9 legs on this box; resolving here runs them.

Deliberately NOT the `real_home` marker -- that un-quarantines HOME for the
whole test, which this needs no part of. One absolute path, captured early,
is the smaller ask."""


def _describe_sample(sample: Optional[dict]) -> str:
    """One failing door sample, rendered for a failure message. Returns a
    literal 'none captured' rather than raising when there is no failing
    sample to describe -- a diagnostic that dies while explaining a failure
    replaces the finding with its own traceback."""
    if sample is None:
        return "none captured"
    return (
        f"rc={sample['rc']} indeterminate={sample['indeterminate']} "
        f"stderr={(sample['stderr'] or '')[:400]!r} "
        f"stdout={(sample['stdout'] or '')[:400]!r}"
    )


def _door_exe_path(engine_root: Path) -> Path:
    """The INSTALLED `coordinator-invoke.exe` -- the binary real callers
    actually dial (AC1), resolved off the settings-home bin ladder rather
    than out of this tree.

    Negative spec: NOT the tree-local `coordinator_core/warm/door/door.exe`,
    even though `_hardlink_coordinator_core` does hardlink it into the
    isolated root. Measured 2026-08-27, same isolated engine and same argv:
    the tree-local build returns `-32004 WARM_DISPATCH_INDETERMINATE` on
    every call including a bare `ping` with no git work, while the installed
    binary answers `ok: true` in 16.6-16.9ms warm. Pointing this helper at
    the tree-local build is what made C9's first run unable to produce any
    figure at all, on either arm.

    The isolated clone is still the code under measurement: the door is only
    a native JSON-RPC framer onto the warm pipe, and `_door_env` below points
    it at `engine_root` via `COORDINATOR_DOOR_ENGINE_ROOT` -- verified
    2026-08-27 (installed binary + that override against this tree: `ok:
    true`). Which framer binary runs is orthogonal to whose engine answers.
    """
    override = os.environ.get(_DOOR_EXE_OVERRIDE_ENV)
    if override:
        return Path(override)
    return _INSTALLED_DOOR_EXE


_DOOR_EXE_OVERRIDE_ENV = "COORDINATOR_DOOR_EXE"
"""Point this file's driver at a specific `coordinator-invoke.exe`. Exists so a
box whose settings-home sits off the default ladder can still run these gates;
the default is the installed door, never a tree-local build (`_door_exe_path`)."""


_DOOR_ENGINE_ROOT_ENV = "COORDINATOR_DOOR_ENGINE_ROOT"
"""`door.c :: resolve_engine_root`'s documented override -- the same env var
`test_door_read_deadline.py :: _run_door` uses to point the door at a
throwaway engine root rather than its baked-in sidecar/build default."""


def _door_env(engine_root: Path, **overrides) -> dict:
    """`_env()` plus the door's own engine-root override -- the door's
    sidecar file (`door.engine-root.txt`), if any, was hardlinked FROM the
    source tree and would resolve back to it, not to this isolated clone;
    the env override is what makes THIS clone the one the door dials."""
    env = _env(**overrides)
    env[_DOOR_ENGINE_ROOT_ENV] = str(engine_root)
    return env


def _invoke_commit_argv(repo: Path, params_path: Path, door_exe: Path) -> List[str]:
    """The DOOR's own argv, timed DIRECTLY -- no driver wrapper, ONE process
    start, and (module docstring, C1) the actual binary real callers dial:
    `coordinator-invoke.exe`, never `python -m coordinator_core.invoke`. The
    two are NOT interchangeable for this measurement -- the interpreter-start
    delta between them (~51-56ms, module docstring) dwarfs AC4's whole
    reported miss, so measuring the Python module form would time a door no
    real caller uses. `argv[0]` is the only change from the prior Python-module
    shape; the door forwards `argv[1:]` verbatim to the same JSON-RPC op,
    unchanged (`door.c` main(): "argv[0] is not forwarded -- only argv[1:]
    crosses the wire"). `--params-file` (not the positional `params_json` argv
    form) is deliberate, not incidental: this driver's params carry a JSON
    object with braces/spaces (`invoke/__main__.py`'s own docstring names
    exactly this payload shape as `--params-file`'s reason to exist --
    quoting-immune, ARG_MAX-safe)."""
    return [
        str(door_exe), "ceremony.commit",
        "--params-file", str(params_path), "--repo", str(repo),
        "--allow-unstamped-dispatch",
    ]


def _write_driver(driver_path: Path, repo: Path, counter_path: Path, isolated_root: Path) -> None:
    """Writes a driver that, IN-PROCESS, mutates `tracked.txt` to fresh
    content (so k identical-argv re-runs each produce a real, distinct
    commit — same idempotent-fixture trap `test_commit_path_process_budget
    .py::_write_driver` names), writes the params object to a sidecar file,
    then spawns the real invoke door via `--params-file` (module docstring:
    quoting-immune for a JSON payload carrying braces/spaces) and forwards
    its stdout/stderr/returncode verbatim.

    STILL USED ONLY for the process-time/spawn-count leg
    (`batched_process_time_ms`), which re-runs the SAME argv k times and so
    has no harness-side loop to mutate content between calls from outside —
    the driver's own extra interpreter start is therefore folded into that
    leg's process-time figure by construction (module docstring: "the driver
    exists because `batched_process_time_ms` re-runs identical argv"). The
    wallclock and concurrent legs below no longer use this: they call
    `_prepare_commit_invocation` + `_invoke_commit_argv` and time the invoke
    door directly, ONE interpreter per sample."""
    script = f'''\
import json
import subprocess
import sys
from pathlib import Path

repo = r"{repo}"
counter_path = Path(r"{counter_path}")
n = int(counter_path.read_text()) if counter_path.exists() else 0
n += 1
counter_path.write_text(str(n))

Path(repo, "tracked.txt").write_text(f"driver rev {{n}}\\n", encoding="utf-8")

params = {{
    "subject": f"c2 wallclock baseline commit {{n}}",
    "stage_paths": ["tracked.txt"],
    "caller_paths": ["tracked.txt"],
    "push_mode": "none",
}}
params_path = counter_path.with_suffix(".params.json")
params_path.write_text(json.dumps(params), encoding="utf-8")

argv = [
    sys.executable, "-m", "coordinator_core.invoke", "ceremony.commit",
    "--params-file", str(params_path), "--repo", repo, "--allow-unstamped-dispatch",
]
completed = subprocess.run(
    argv, capture_output=True, text=True,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
sys.exit(completed.returncode)
'''
    driver_path.write_text(script, encoding="utf-8")


def _parse_invoke_stdout(stdout: str) -> dict:
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), f"invoke stdout was not a JSON object: {stdout!r}"
    assert "error" not in parsed, f"ceremony.commit returned an error envelope: {stdout!r}"
    return parsed


def _percentile(ordered: List[float], pct: float) -> float:
    """Nearest-rank, round-half-up — matches `process_time.py ::
    batched_process_time_quantiles._percentile`'s own tie-break rationale
    (Python's `round()` is ties-to-even, which silently mis-picks at small n)."""
    if len(ordered) == 1:
        return ordered[0]
    idx = math.floor(pct * (len(ordered) - 1) + 0.5)
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def _wallclock_samples(
    cmd: List[str],
    k: int,
    cwd: str,
    env: dict,
    prepare: Optional[object] = None,
) -> dict:
    """Spawn-to-exit wall clock, one sample per real invocation (never a
    batched/amortised figure — AC4/AC7 need PER-CALL quantiles, not a mean).
    Verifies rc==0 and a real (non-error) JSON-RPC envelope for every
    sample, same AC9-style discipline `timer.py::time_invocation` applies.

    `cmd` is the invoke door argv directly (module docstring: "time the
    invoke door argv directly") -- ONE interpreter start per sample, never a
    driver wrapper. `prepare`, if given, runs once per iteration BEFORE the
    clock starts -- the harness-side content mutation the module docstring
    requires stay OUTSIDE the timed window, exactly as the spike's own
    ladder does."""
    samples_ms: List[float] = []
    for _ in range(k):
        if prepare is not None:
            prepare()
        t0 = time.perf_counter()
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert completed.returncode == 0, (
            f"driver invocation failed rc={completed.returncode}: "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
        _parse_invoke_stdout(completed.stdout)
        samples_ms.append(elapsed_ms)

    ordered = sorted(samples_ms)
    return {
        "n": k,
        "samples_ms": samples_ms,
        "median_ms": round(statistics.median(ordered), 3),
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


@pytest.mark.parametrize("index_size", INDEX_SIZES, ids=INDEX_SIZE_IDS)
def test_commit_op_wallclock_and_spawn_baseline_are_recorded(
    tmp_path_factory, warm_engine_root, request, index_size
) -> None:
    """AC4's instrument, now GATED (C6 — module docstring's "MEASURED
    2026-08-26" note) and parameterised over index size (C4 dispatch
    brief). Three separate columns from three separate measurement passes
    over the same driver/argv: wallclock quantiles (this pass,
    spawn-to-exit, k=15), process time, and job-object spawn count (a
    second pass, `batched_process_time_ms`, k=8) — never collapsed into one
    figure; only the wallclock column carries AC4's numeric gate, at BOTH
    index sizes -- the large-index case is expected to fail it (module
    docstring's FIXTURE-SIZE PARAMETERISATION note), and that failure is
    this chunk's correct outcome, not softened here.
    """
    tmp_path = tmp_path_factory.mktemp("commit_wallclock")
    base_repo = (
        request.getfixturevalue("large_index_base_repo") if index_size > 1 else None
    )
    repo = _build_fixture_repo(tmp_path, "work/c2-wallclock-baseline", base_repo=base_repo)
    door_exe = _door_exe_path(warm_engine_root)
    if not door_exe.exists():
        pytest.skip(
            f"{door_exe} not present -- this box has no installed "
            f"coordinator-invoke.exe on the settings-home ladder; point "
            f"{_DOOR_EXE_OVERRIDE_ENV} at one to run this gate "
            "(see module docstring's C1 note)"
        )
    env = _door_env(warm_engine_root)

    wall_counter = tmp_path / "wall_counter.txt"
    wall_params = _prepare_commit_invocation(repo, wall_counter)
    wall_argv = _invoke_commit_argv(repo, wall_params, door_exe)
    wall = _wallclock_samples(
        wall_argv,
        k=K_WALLCLOCK_SAMPLES,
        cwd=str(warm_engine_root),
        env=env,
        prepare=lambda: _prepare_commit_invocation(repo, wall_counter),
    )

    proc_driver = tmp_path / "driver_proc.py"
    proc_counter = tmp_path / "proc_counter.txt"
    _write_driver(proc_driver, repo, proc_counter, warm_engine_root)
    proc = batched_process_time_ms(
        [sys.executable, str(proc_driver)],
        k=K_PROCESS_TIME_INVOCATIONS,
        cwd=str(warm_engine_root),
        env=env,
    )

    detail = (
        f"AC4 warm-served baseline (index_size={index_size}, warmth asserted "
        f"by warm_engine_root's _assert_warmth_or_fail, not merely requested "
        f"-- see that helper's docstring): "
        f"wallclock median={wall['median_ms']}ms p50={wall['p50_ms']}ms "
        f"p95={wall['p95_ms']}ms min={wall['min_ms']}ms max={wall['max_ms']}ms "
        f"(n={wall['n']}). process_time={proc['process_time_ms']}ms "
        f"procs_per_call={proc['procs_per_call']} (k={proc['k']})."
    )
    print(detail)
    assert proc["rc"] == 0, f"process-time leg's driver must exit 0: {proc!r}. {detail}"
    assert wall["n"] == K_WALLCLOCK_SAMPLES
    assert wall["median_ms"] > 0.0, f"a zero-ms wallclock sample means the instrument is not measuring anything. {detail}"
    assert proc["procs_per_call"] >= 1.0, f"the op's own interpreter must count as at least one process. {detail}"
    # AC4 (C6): the real numeric gate, not a recorded-only baseline. See
    # module docstring's "MEASURED 2026-08-26" note for the current delta
    # and why it is a mechanism gap outside this chunk's writes: scope
    # rather than a loosening candidate.
    assert wall["median_ms"] <= AC4_TARGET_MS, (
        f"AC4 FAILS: wallclock median {wall['median_ms']}ms exceeds the "
        f"{AC4_TARGET_MS}ms target by {round(wall['median_ms'] - AC4_TARGET_MS, 3)}ms. {detail}"
    )


def test_commit_op_dial_leg_process_time_is_recorded(warm_engine_root) -> None:
    """AC5's instrument: the cost of REACHING the engine alone, reported
    separately from AC4's whole-commit total (module docstring). `ping` is
    a "none"-scoped op with zero git work -- the cheapest real round trip
    through the same warm-attempt-then-cold-fallback door AC4 measures,
    isolating dial cost from commit cost.
    """
    result = batched_process_time_ms(
        [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
        k=K_PROCESS_TIME_INVOCATIONS,
        cwd=str(warm_engine_root),
    )
    AC5_TARGET_MS = 60.0
    """DR-347 Ruling 1's amended figure (plan Problem section: "AC5 uses
    ~60ms"). Reported here, NOT gated: this AC is PROVISIONAL per this
    chunk's own brief, pending the sibling plan named in the parent plan's
    § Blocked by (docs/plans/2026-08-26-the-op-clis-dial-warm-from-the-
    process.md, chunks C1/C5/C6/C7/C9 landed per that plan's own tracker)."""
    delta = round(result["process_time_ms"] - AC5_TARGET_MS, 3)
    print(
        f"AC5 dial leg (PROVISIONAL, ungated -- see docstring): "
        f"process_time={result['process_time_ms']}ms procs_per_call="
        f"{result['procs_per_call']} vs ~{AC5_TARGET_MS}ms target (delta={delta}ms)"
    )
    assert result["rc"] == 0, f"AC5 dial-leg ping must exit 0: {result!r}"
    assert result["process_time_ms"] >= 0.0, (
        f"AC5 dial-leg baseline (reported separately from AC4's total, "
        f"PROVISIONAL -- see docstring): "
        f"process_time={result['process_time_ms']}ms procs_per_call="
        f"{result['procs_per_call']} (k={result['k']}) vs ~{AC5_TARGET_MS}ms "
        f"target (delta={delta}ms)"
    )


def _drain_concurrent_samples(
    procs: List[subprocess.Popen],
    starts: List[float],
    timeout: float,
) -> List[tuple]:
    """One waiter thread per proc, stamping THAT proc's own completion the
    instant its own `communicate()` returns -- never the drain loop's turn
    (module docstring's CONCURRENT-DRAIN FIX). A plain launch-order loop
    computes sample i's elapsed only after every earlier proc has already
    been drained, so a slow proc 0 raises every later sample's floor to at
    least its own finish time. Returns `(rc, stdout, stderr, elapsed_ms)`
    tuples in the SAME order as `procs`/`starts`, regardless of which
    thread actually finishes first."""
    results: List[Optional[tuple]] = [None] * len(procs)

    def _wait(i: int, proc: subprocess.Popen, start: float) -> None:
        stdout, stderr = proc.communicate(timeout=timeout)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        results[i] = (proc.returncode, stdout, stderr, elapsed_ms)

    threads = [
        threading.Thread(target=_wait, args=(i, proc, start))
        for i, (proc, start) in enumerate(zip(procs, starts))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 5)
    for i, r in enumerate(results):
        assert r is not None, f"waiter thread for proc index {i} never completed"
    return results


def test_concurrent_drain_isolates_each_procs_own_exit() -> None:
    """C2's own pinned regression: a deliberately-slow FIRST proc must not
    raise a LATER, fast proc's measured elapsed. Launches N procs where
    proc 0 sleeps far longer than the rest, drains them all through
    `_drain_concurrent_samples`, and asserts the fast procs' own samples
    stay near their own sleep time rather than being floored at proc 0's
    finish time -- the exact failure mode the module docstring's
    CONCURRENT-DRAIN FIX note names."""
    sleep_secs = [1.5, 0.05, 0.05, 0.05]
    argv_for = lambda s: [sys.executable, "-c", f"import time; time.sleep({s})"]

    procs: List[subprocess.Popen] = []
    starts: List[float] = []
    for s in sleep_secs:
        starts.append(time.perf_counter())
        procs.append(
            subprocess.Popen(
                argv_for(s),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        )

    results = _drain_concurrent_samples(procs, starts, timeout=_SUBPROCESS_TIMEOUT_S)

    for rc, _stdout, stderr, _elapsed_ms in results:
        assert rc == 0, f"sleep subprocess failed rc={rc}: stderr={stderr!r}"

    slow_elapsed_ms = results[0][3]
    fast_elapsed_ms = [r[3] for r in results[1:]]
    detail = (
        f"slow_elapsed_ms={slow_elapsed_ms} fast_elapsed_ms={fast_elapsed_ms} "
        f"sleep_secs={sleep_secs}"
    )
    for fast_ms in fast_elapsed_ms:
        assert fast_ms < slow_elapsed_ms - 500.0, (
            f"a fast proc's own sample must not be raised to near the slow "
            f"first proc's finish time -- if it is, the drain is stamping "
            f"the loop's own turn, not the proc's own exit. {detail}"
        )
        assert fast_ms < 1000.0, (
            f"a 0.05s-sleep proc measured >=1s elapsed -- its sample was "
            f"inflated by an earlier proc's drain order. {detail}"
        )


@pytest.mark.parametrize("index_size", INDEX_SIZES, ids=INDEX_SIZE_IDS)
def test_commit_op_concurrent_load_wallclock_is_recorded(
    tmp_path_factory, warm_engine_root, request, index_size
) -> None:
    """AC7's instrument, now GATED on p50 (C6 -- module docstring's "MEASURED
    2026-08-26" note) and parameterised over index size (C4 dispatch
    brief): >=8 simultaneous commits from INDEPENDENT repos (own `git
    init` or `git clone --local` off one once-built large-index base, own
    object store and refs -- never `git worktree add` off one shared base
    repo, module docstring's C3 WORKTREE FIX), all dialing the SAME
    isolated warm server, so engine queueing is the term under
    measurement, unconfounded by a shared object store. Reports wallclock
    p50 AND p95 -- p95 is exposition only (queueing tail latency a p50/mean
    would hide); AC7's own text ("AC4's wallclock holds ... under
    concurrent load") gates p50 against AC4_TARGET_MS, the same figure AC4
    itself gates, at BOTH index sizes -- the large-index case is expected
    to fail it (module docstring's FIXTURE-SIZE PARAMETERISATION note).
    """
    tmp_path = tmp_path_factory.mktemp("commit_wallclock_concurrent")
    door_exe = _door_exe_path(warm_engine_root)
    if not door_exe.exists():
        pytest.skip(
            f"{door_exe} not present -- this box has no installed "
            f"coordinator-invoke.exe on the settings-home ladder; point "
            f"{_DOOR_EXE_OVERRIDE_ENV} at one to run this gate "
            "(see module docstring's C1 note)"
        )
    env = _door_env(warm_engine_root)
    base_repo = (
        request.getfixturevalue("large_index_base_repo") if index_size > 1 else None
    )

    repos: List[Path] = []
    for i in range(N_CONCURRENT):
        repo_tmp = tmp_path / f"repo-{i}"
        repo_tmp.mkdir()
        repo = _build_fixture_repo(
            repo_tmp, f"work/c2-wallclock-concurrent-{i}", base_repo=base_repo
        )
        repos.append(repo)

    argvs = []
    for i, repo in enumerate(repos):
        counter = tmp_path / f"concurrent_counter_{i}.txt"
        params_path = _prepare_commit_invocation(repo, counter)
        argvs.append(_invoke_commit_argv(repo, params_path, door_exe))

    procs = []
    starts = []
    for argv in argvs:
        starts.append(time.perf_counter())
        procs.append(
            subprocess.Popen(
                argv,
                cwd=str(warm_engine_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        )

    # Every proc is drained BEFORE any assertion fires -- a mid-loop assert
    # on the first non-zero rc would leave later procs' pipes unread and
    # would under-count indeterminate_count for a run where more than one
    # of the N_CONCURRENT callers hit -32004 (brief: "assert 0 -32004
    # responses as a criterion in its own right, not as an incidental").
    # One waiter thread per proc (`_drain_concurrent_samples`, module
    # docstring's CONCURRENT-DRAIN FIX) -- each sample's elapsed is stamped
    # at ITS OWN `communicate()` return, never raised to the max of every
    # earlier proc's finish by a plain launch-order drain loop.
    results = _drain_concurrent_samples(procs, starts, timeout=_SUBPROCESS_TIMEOUT_S)

    # WARM_DISPATCH_INDETERMINATE (-32004, `warm/client.py`) parsed
    # defensively: a -32004 envelope is still valid JSON on stdout even
    # though the process's own returncode is non-zero for it.
    indeterminate_count = 0
    for rc, stdout, _stderr, _elapsed_ms in results:
        try:
            parsed = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict) and err.get("code") == -32004:
                indeterminate_count += 1

    samples_ms = [r[3] for r in results]
    ordered = sorted(samples_ms)
    p50 = round(_percentile(ordered, 0.50), 3)
    p95 = round(_percentile(ordered, 0.95), 3)
    detail = (
        f"AC7 warm-served concurrent-load baseline (index_size={index_size}, "
        f"n={len(ordered)} independent repos, warmth asserted by "
        f"warm_engine_root's _assert_warmth_or_fail, not merely requested): "
        f"wallclock p50={p50}ms p95={p95}ms "
        f"min={round(ordered[0], 3)}ms max={round(ordered[-1], 3)}ms samples={ordered} "
        f"-32004 (WARM_DISPATCH_INDETERMINATE) responses={indeterminate_count}/{N_CONCURRENT}"
    )
    print(detail)

    # AC7's own criterion, in its own right (brief): a run where the op's
    # outcome is unknowable under ordinary concurrent load has not met a
    # concurrency criterion, regardless of what the wallclock figure reads.
    # Checked BEFORE the generic per-call rc==0 assertion below so a -32004
    # failure is never misreported as an undifferentiated "driver failed".
    assert indeterminate_count == 0, (
        f"AC7 FAILS: {indeterminate_count}/{N_CONCURRENT} concurrent commits "
        f"returned -32004 WARM_DISPATCH_INDETERMINATE -- the op's outcome was "
        f"unknowable for at least one caller under {N_CONCURRENT}-way independent-"
        f"repo load. {detail}"
    )
    for rc, stdout, stderr, _elapsed_ms in results:
        assert rc == 0, (
            f"concurrent driver failed rc={rc}: stdout={stdout!r} stderr={stderr!r}. {detail}"
        )
        _parse_invoke_stdout(stdout)
    assert len(ordered) == N_CONCURRENT, detail
    assert p95 >= p50, detail
    assert p50 > 0.0, f"a zero-ms concurrent sample means the instrument is not measuring anything. {detail}"
    # AC7 (C6): "AC4's wallclock holds under concurrent load" -- the same
    # target as AC4, read against p50 (p95 stays reported-only above: its
    # job is exposing queueing tail latency, not carrying a second gate).
    assert p50 <= AC4_TARGET_MS, (
        f"AC7 FAILS: concurrent-load wallclock p50 {p50}ms exceeds AC4's "
        f"{AC4_TARGET_MS}ms target by {round(p50 - AC4_TARGET_MS, 3)}ms under "
        f"{N_CONCURRENT}-way independent-repo load. {detail}"
    )


def _head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


FLOOR_MS = 78.1
"""`## The route to the floor`'s own measured floor: gate-closed, 1 git
spawn / 2 procs -- what a three-path commit irreducibly costs. Reported
against, never gated on (dispatch brief: "Not a pass/fail against 500ms,
290ms or 150ms -- those are ceilings")."""


def _run_door_sample(
    argv: List[str],
    cwd: str,
    env: dict,
    accountant: Optional[LiveTreeAccountant] = None,
) -> dict:
    """One door invocation, timed, WITHOUT asserting rc==0 -- C9's arm
    verdict must be able to observe and report a -32004
    WARM_DISPATCH_INDETERMINATE response rather than treating it as an
    instrument bug (dispatch brief door doc: "the op may have COMPLETED").
    Returns rc, stdout, parsed envelope (or None), elapsed_ms, and whether
    the response was the door's own indeterminate code.

    `accountant` supplies the axis the brightline actually applies to.
    `elapsed_ms` is `perf_counter()` wall clock, which on a box running
    50-70 concurrent sessions is substantially a reading of PEER LOAD --
    CLAUDE.md ("process time and spawn count, never wall clock"), DR-344
    and three reviewer sidecars all exclude it, and a verdict taken on it
    is not a verdict. It stays reported as context, never as the result.

    A `LiveTreeAccountant` attached to the warm SERVER brackets this one
    invocation with a job-object read, so `process_time_ms` and `procs`
    describe the OP's own CPU and its real spawn count -- including every
    `git` child and the `conhost.exe` DR-373 found beside each one. None of
    those appear in the client framer's own process tree, which is why
    measuring the client and calling it the op reports the framer.
    """
    before = accountant.snapshot() if accountant is not None else None
    t0 = time.perf_counter()
    completed = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=_SUBPROCESS_TIMEOUT_S, creationflags=_CREATE_NO_WINDOW,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    after = accountant.snapshot() if accountant is not None else None
    try:
        parsed = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    indeterminate = (
        isinstance(parsed, dict)
        and isinstance(parsed.get("error"), dict)
        and parsed["error"].get("code") == -32004
    )
    return {
        "rc": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "parsed": parsed,
        "elapsed_ms": round(elapsed_ms, 3),
        "process_time_ms": (
            round(after["process_time_ms"] - before["process_time_ms"], 3)
            if before is not None and after is not None
            else None
        ),
        "procs": (
            after["procs"] - before["procs"]
            if before is not None and after is not None
            else None
        ),
        "indeterminate": indeterminate,
    }


@pytest.mark.parametrize("index_size", INDEX_SIZES, ids=INDEX_SIZE_IDS)
def test_c9_route_table_verdict_per_arm(
    tmp_path_factory, warm_engine_root, server_accountant, request, index_size
) -> None:
    """C9 -- the plan's own deliverable. Reports `## The route to the
    floor`'s R1-R5 through the real door (`coordinator-invoke.exe`), PER
    ARM, never blended (dispatch brief: "A total that does not name its arm
    is not a result").

    ARM SPLIT, real, not simulated: `_run_in_plane_archive_sweep`'s own
    cadence gate (`commit_pipeline.py::_archive_sweep_due`/
    `_stamp_archive_sweep`) keys off a marker under this FIXTURE REPO's own
    `<common_dir>/coordinator-sessions/archive-terminal-handoffs.cadence`,
    absent on a fresh `git init`/`clone`. The FIRST commit against a fresh
    repo is therefore unconditionally GATE-OPEN (the sweep's cadence pass
    fires and stamps the marker); every commit after it inside the 15-minute
    interval this run stays inside is GATE-CLOSED (the common commit). The
    fixture repo carries no `state/handoffs`/`cross-repo/inbox` corpus, so
    the sweep's three legs find nothing to move on either arm -- this
    measures the cadence-gate's classification/dirty-check MARGINAL cost
    (what the route table's "sweep fires" vs "sweep idle" actually differ
    by), not a production-sized corpus walk; recorded honestly as a
    limitation of this fixture, not hidden.

    AC9: for EVERY sample on both arms, three claims are checked, never
    collapsed into one -- rc==0, a `committed: true`-shaped envelope
    (`"error" not in parsed`), and HEAD actually moving to a DISTINCT sha.
    A -32004 sample is not asserted rc==0 (module docstring's -32004
    finding, AC7); it is recorded as indeterminate and HEAD is inspected
    anyway, since the op may have completed despite the response.
    """
    tmp_path = tmp_path_factory.mktemp("commit_wallclock_c9")
    base_repo = (
        request.getfixturevalue("large_index_base_repo") if index_size > 1 else None
    )
    repo = _build_fixture_repo(tmp_path, "work/c9-route-verdict", base_repo=base_repo)
    door_exe = _door_exe_path(warm_engine_root)
    if not door_exe.exists():
        pytest.skip(
            f"{door_exe} not present -- this box has no installed "
            f"coordinator-invoke.exe on the settings-home ladder; point "
            f"{_DOOR_EXE_OVERRIDE_ENV} at one to run this gate "
            "(see module docstring's C1 note)"
        )
    env = _door_env(warm_engine_root)
    counter = tmp_path / "c9_counter.txt"

    samples = []
    for i in range(K_WALLCLOCK_SAMPLES):
        head_before = _head_sha(repo)
        params_path = _prepare_commit_invocation(repo, counter)
        argv = _invoke_commit_argv(repo, params_path, door_exe)
        result = _run_door_sample(
            argv, cwd=str(warm_engine_root), env=env, accountant=server_accountant
        )
        head_after = _head_sha(repo)
        result["arm"] = "gate-open" if i == 0 else "gate-closed"
        result["head_moved"] = head_after != head_before
        # `committed` sits under the JSON-RPC envelope's `result` member, not at
        # its top level -- reading it off the envelope root records
        # committed_true=False against a payload that plainly says
        # `"result": {"committed": true}`, which is AC9's "a `committed: true`
        # envelope" claim failing on the reader rather than on the op.
        parsed = result["parsed"]
        envelope_result = parsed.get("result") if isinstance(parsed, dict) else None
        result["committed_true"] = (
            isinstance(envelope_result, dict)
            and envelope_result.get("committed") is True
        )
        samples.append(result)

    def _arm_report(arm: str) -> dict:
        arm_samples = [s for s in samples if s["arm"] == arm]
        clean = [s for s in arm_samples if s["rc"] == 0 and not s["indeterminate"]]
        elapsed = sorted(s["elapsed_ms"] for s in clean)
        median_ms = round(statistics.median(elapsed), 3) if elapsed else None
        # The result axis. `distance_to_floor_ms` hangs off THIS median and
        # never off the wallclock one: FLOOR_MS is a job-accounted figure, and
        # subtracting it from a `perf_counter` median produced a "+380.3ms
        # distance to floor" with a decimal point and no referent -- two kinds
        # of number in one column.
        proc_times = sorted(
            s["process_time_ms"] for s in clean if s["process_time_ms"] is not None
        )
        proc_median_ms = (
            round(statistics.median(proc_times), 3) if proc_times else None
        )
        procs = [s["procs"] for s in clean if s["procs"] is not None]
        return {
            "arm": arm,
            "n": len(arm_samples),
            "n_clean": len(clean),
            "n_indeterminate": sum(1 for s in arm_samples if s["indeterminate"]),
            "process_time_median_ms": proc_median_ms,
            "procs_per_call": (
                round(statistics.mean(procs), 3) if procs else None
            ),
            "procs_samples": procs,
            "wallclock_median_ms": median_ms,
            "median_ms": median_ms,
            "distance_to_floor_ms": (
                round(proc_median_ms - FLOOR_MS, 3)
                if proc_median_ms is not None
                else None
            ),
            "head_moved_all": all(s["head_moved"] for s in arm_samples) if arm_samples else None,
            "committed_true_all": (
                all(s["committed_true"] for s in arm_samples if s["rc"] == 0)
                if any(s["rc"] == 0 for s in arm_samples) else None
            ),
        }

    open_report = _arm_report("gate-open")
    closed_report = _arm_report("gate-closed")

    # R4 -- counted, not timed (disposition c10: closed at 4.47ms once-per-
    # PROCESS, ~0% warm by construction; a warm server pays it once per
    # server lifetime, not per commit, so it is not re-measured per sample
    # here). R5 -- counted via the job object, never timed (dispatch brief:
    # "R5 is a count question outright: procs_per_call either is 2.00 on
    # every commit or it is not"), reusing the same batched_process_time_ms
    # leg the AC4 test already runs.
    proc_driver = tmp_path / "c9_driver_proc.py"
    proc_counter = tmp_path / "c9_proc_counter.txt"
    _write_driver(proc_driver, repo, proc_counter, warm_engine_root)
    proc = batched_process_time_ms(
        [sys.executable, str(proc_driver)],
        k=K_PROCESS_TIME_INVOCATIONS,
        cwd=str(warm_engine_root),
        env=env,
    )
    r5_procs_per_call = proc["procs_per_call"]

    def _arm_line(r: dict) -> str:
        return (
            f"{r['arm']}: n={r['n']} n_clean={r['n_clean']} "
            f"n_indeterminate={r['n_indeterminate']} "
            f"PROCESS_TIME_median_ms={r['process_time_median_ms']} "
            f"procs_per_call={r['procs_per_call']} (samples={r['procs_samples']}) "
            f"distance_to_floor_ms={r['distance_to_floor_ms']} (floor={FLOOR_MS}ms) "
            f"[context only, NOT the verdict axis: "
            f"wallclock_median_ms={r['wallclock_median_ms']}] "
            f"head_moved_all={r['head_moved_all']} "
            f"committed_true_all={r['committed_true_all']}."
        )

    verdict = (
        f"C9 VERDICT (index_size={index_size}), axis=PROCESS TIME "
        f"(job-object, server-attached; CLAUDE.md/DR-344 exclude wall clock). "
        f"{_arm_line(open_report)} {_arm_line(closed_report)} "
        f"R1+R2+R3 (191.5ms cold, dlv-ceremony-restore-01-770dd6): unverified "
        f"at this door -- this plan's scope excludes the op-side files those "
        f"rows would land in. "
        f"R4 (call-time imports): CLOSED, not cut -- 4.47ms once-per-PROCESS, "
        f"~0% warm by construction, not re-measured per-commit here. "
        f"R5 (conhost.exe per git child): procs_per_call={r5_procs_per_call} "
        f"(k={proc['k']}) -- 2.00 means the confirmed ~47ms/commit row is "
        f"UNVERIFIED AT THIS DOOR (job-object process count, not conhost "
        f"count specifically; DR-373 owns the cut)."
    )
    print(verdict)

    # A SYSTEMIC door failure (every sample on every arm indeterminate) is a
    # different, stronger finding than "AC9 failed for one sample": it means
    # this box currently cannot produce ANY door-measured number at all, not
    # merely a number that misses a target. Named explicitly rather than
    # left to read as an unattributed per-sample AC9 assertion below.
    if open_report["n_clean"] == 0 and closed_report["n_clean"] == 0:
        # Report WHICH failure this is rather than asserting one. An earlier
        # revision hardcoded "every sample returned -32004, therefore door.c is
        # broken" into this message; it then printed that diagnosis verbatim on
        # a run whose samples were not indeterminate at all (n_indeterminate=0,
        # rc!=0), which is this file's own subject matter -- an instrument that
        # can only return one answer measures nothing. Carry the evidence.
        n_indet = open_report["n_indeterminate"] + closed_report["n_indeterminate"]
        n_total = len(samples)
        worst = next(
            (s for s in samples if s["rc"] != 0 or s["indeterminate"]), None
        )
        pytest.fail(
            f"C9 INSTRUMENT FAILURE (index_size={index_size}): no clean sample "
            f"on EITHER arm -- {n_indet}/{n_total} indeterminate (-32004), "
            f"{n_total - n_indet}/{n_total} failed for another reason. This box "
            f"cannot currently produce ANY door-measured figure, which is a "
            f"stronger and different finding than a number that misses a "
            f"target. First failing sample: {_describe_sample(worst)}. {verdict}"
        )

    # AC9: three separate claims, per sample, never collapsed.
    for s in samples:
        assert s["head_moved"], (
            f"AC9 FAILS ({s['arm']} arm, index_size={index_size}): HEAD did "
            f"not move to a distinct SHA for a sample. {verdict}\nsample={s!r}"
        )
        if s["rc"] == 0:
            assert s["committed_true"], (
                f"AC9 FAILS ({s['arm']} arm, index_size={index_size}): rc==0 "
                f"but no committed:true envelope. {verdict}\nsample={s!r}"
            )

    # This row's own headline (dispatch brief: "Then the headline: distance
    # to 78.1ms, per arm, with the outstanding rows named. Not a pass/fail
    # against 500ms, 290ms or 150ms"). Deliberately NOT a numeric gate on
    # FLOOR_MS or on any of DR-344/DR-368/the predecessor plan's ceilings --
    # "A miss is a result, not a failure of this plan." The only hard
    # assertions in this test are AC9's three-claim correctness check above;
    # every wallclock/count figure here is REPORTED via `verdict`, read from
    # this test's own captured stdout, never gated.
    assert open_report["n"] == 1, f"exactly one gate-open sample by construction. {verdict}"
    assert closed_report["n"] == K_WALLCLOCK_SAMPLES - 1, verdict


def test_invoke_commit_argv_uses_the_door_binary_not_the_python_module(tmp_path: Path) -> None:
    """AC1's own structural assertion (dispatch brief: "Assert the argv
    structurally. A comment naming which door it uses is what let this drift
    in the first place."). Pinned directly on `_invoke_commit_argv`'s own
    output, independent of any warm server or isolated clone -- placeholder
    `tmp_path`-rooted paths stand in for repo/params/door_exe since only argv
    SHAPE is under test here, never whether the path exists or dials
    anything."""
    repo = tmp_path / "not-a-real-repo"
    params_path = tmp_path / "not-a-real-repo" / "params.json"
    door_exe = tmp_path / "isolated-root" / "coordinator_core" / "warm" / "door" / "door.exe"

    argv = _invoke_commit_argv(repo, params_path, door_exe)

    assert argv[0] == str(door_exe), (
        f"argv[0] must be the door binary itself, not an interpreter: {argv!r}"
    )
    assert sys.executable not in argv, (
        f"the door's own argv must never carry this process's interpreter path: {argv!r}"
    )
    assert "-m" not in argv, (
        f"the door's own argv must never carry the '-m' module-invocation flag "
        f"real callers do not use: {argv!r}"
    )
    assert "coordinator_core.invoke" not in argv, (
        f"the door's own argv must never name the Python module form: {argv!r}"
    )
    assert argv[1] == "ceremony.commit"
    assert "--params-file" in argv and str(params_path) in argv
    assert "--repo" in argv and str(repo) in argv
    assert "--allow-unstamped-dispatch" in argv
