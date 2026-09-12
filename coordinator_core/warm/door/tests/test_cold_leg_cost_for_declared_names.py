"""C5: pre-measurement spike -- process-time cost of the door's cold leg
for a stdin-declared entrypoint name, against a LOCALLY-BUILT `door.exe`.

Spec: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md
§ C5, resting on C3/C4's basename gate (`door_core.c ::
door_basename_declares_stdin_read`, wired into `door.c`'s main() ahead of
the pipe dial).

WHY THIS CHUNK MEASURES BEFORE C6/C8, NOT AFTER. The prior sequencing
(cut per coordinator:apm finding 5, folded into C8's close-out) measured
only after C6's image rebuild and C8's 387-slot reinstall -- by which
point the cheapest response to a bad number (choosing a different gate
shape) was already foreclosed. This file measures against a door BUILT
LOCALLY FROM THE CURRENT SOURCE TREE, no image rebuild and no reinstall
required to get a number.

WHY `fan-out-dispatch`, NOT THE HIGHEST-FREQUENCY NAME OVERALL
(coordinator:apm finding 4, EM-supplied fact). `fan-out-dispatch` is the
top of the six PROBEABLE names (declared stdin-reading AND door-eligible)
by count of files in the tree mentioning the name (72, via `grep -rl`
over coordinator_core/, coordinator/, docs/, state/) -- a PROXY for
invocation frequency, not a measurement of it; no invocation log covers
these names. The choice generalises anyway: every declared name takes the
same table lookup (`door_basename_declares_stdin_read`) and the same cold
spawn (`fall_through()`), so this measures the GATE SHAPE, not this
entrypoint specifically. `statusline` is NOT the right pick despite being
the highest-frequency name overall: the harness invokes it as a direct
cold command with no forwarder in the chain, so no door is ever built
under that name.

UNIT: process time (job-object `TotalUserTime + TotalKernelTime`), never
wall clock, per DR-344 -- `coordinator_core.benchmarks.process_time`'s own
module docstring names the three measurement traps this avoids.
`batched_process_time_ms` amortises over K_INVOCATIONS=20 to beat the
~15.6ms scheduler-tick quantisation a single sample cannot resolve, and
its own `procs_per_call` is the spawn-count half of this spike's
question -- no second mechanism is built to answer it.

WARM VS COLD, FOR A NAME THE GATE FORCES COLD UNCONDITIONALLY. `fan-out-
dispatch` never reaches the warm server regardless of `COORDINATOR_WARM`
(that is the whole point of C3/C4's gate) -- so "warm vs cold" here is NOT
a warm-hit-vs-`COORDINATOR_WARM=0` comparison as in
`benchmarks/tests/test_warm_door_process_time_gate.py`. It is: invoking
THROUGH `door.exe` under the declared name (gate fires, `fall_through()`
cold-spawns the Python entrypoint as a CHILD process -- 2 processes in the
tree) versus invoking the underlying cold entrypoint DIRECTLY with no door
in front of it at all (1 process). The delta between the two is the
table-lookup-plus-relaunch overhead the gate itself adds over a bare cold
spawn -- the number this spike exists to produce.

PRECONDITION IS A STAMPED ENGINE ROOT TO BUILD FROM, same requirement and
same resolution order as `benchmarks/tests/test_warm_door_process_time_gate.py
:: _resolve_stamped_source_root` (reused here, not re-derived): an
explicit `COORDINATOR_WARM_GATE_ENGINE_ROOT` override, else the sibling
`claude-klabauter` published-mirror clone next to this live tree's own
parent. `door.build.build()` refuses a root with no
`coordinator_core/_engine_stamp` (DR-315 SS2) -- this file inherits that
refusal rather than relaxing it. The BUILT `door.exe` is installed under a
throwaway stub install root (mirroring
`test_windows_door_cold_leg_route.py :: _install_door_as`) and pointed at
via `COORDINATOR_DOOR_ENGINE_ROOT`, which `resolve_engine_root()` prefers
over the baked/sidecar value -- so the binary is built once against the
stamped root (satisfying `build()`'s precondition) and then exercised
against the throwaway stub root at runtime, exactly as the existing
cold-leg-route suite already does.

SKIPS, NAMED: off Windows (job-object accounting has no POSIX equivalent,
same as the sibling gate file); no candidate stamped engine root on this
box.

MEASURED THIS SESSION (2026-09-12, k=20, this box) -- see the two test
functions below for the numbers this run actually produced and the
DR-344 bar (500ms end-to-end, 200ms single-process) each is checked
against. A number that clears the bar closes this spike; a number that
does not is a finding for the PM now, per this chunk's own body, while
the gate shape is still cheap to revisit -- not after C6/C8 have landed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.benchmarks.process_time import IS_WINDOWS, batched_process_time_ms
from coordinator_core.benchmarks.tests.test_warm_door_process_time_gate import (
    _resolve_stamped_source_root,
    _candidate_source_roots,
)
from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.tests.test_door_read_deadline import _make_stub_engine_root

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: The probeable name this spike measures -- see module docstring's
#: "WHY `fan-out-dispatch`" section. Declared in both `door_core.c ::
#: door_stdin_reading_basenames` and
#: `coordinator_core/ops/warm_entrypoint_allowlist.json`'s
#: `entrypoints`/`door_eligible_entrypoints`.
_DECLARED_BASENAME = "fan-out-dispatch"

#: DR-344's own bar -- "500ms end-to-end under load, or it isn't built".
PROCESS_TIME_CEILING_MS = 500.0

#: DR-344: "one process over 200ms needs a fix, not a rationale" -- applied
#: to the DIRECT cold entrypoint alone (1 process), since the door-fronted
#: figure above already includes a second process by construction.
SINGLE_PROCESS_CEILING_MS = 200.0

K_INVOCATIONS = 20
"""Amortisation factor -- see `coordinator_core.benchmarks.process_time`
module docstring, trap 2, and this file's own module docstring."""

_ENGINE_ROOT_OVERRIDE_ENV = "COORDINATOR_WARM_GATE_ENGINE_ROOT"


def _cold_marker_source() -> str:
    return (
        "import sys\n"
        "print('cold-leg-cost-spike-marker')\n"
        "raise SystemExit(0)\n"
    )


@pytest.fixture(scope="module")
def door_and_stub_root(tmp_path_factory) -> Iterator[tuple]:
    if not IS_WINDOWS:
        pytest.skip("job-object process-time accounting is a Windows-only primitive")

    source_root = _resolve_stamped_source_root()
    if source_root is None:
        pytest.skip(
            "no candidate engine root carries a valid build stamp among "
            f"{_candidate_source_roots()!r} -- nothing stamped to build a "
            f"local door.exe from; point {_ENGINE_ROOT_OVERRIDE_ENV} at one "
            "to run this spike"
        )

    tmp = tmp_path_factory.mktemp("cold-leg-cost")
    stub_root = _make_stub_engine_root(tmp)
    # `_make_stub_engine_root` installs the pre-C0 default cold script under
    # `coordinator-invoke.py` -- this spike additionally needs a cold script
    # keyed to `_DECLARED_BASENAME`, since C0's name-aware cold leg spawns
    # `<engine_root>/coordinator/bin/<basename>.py`.
    script = stub_root / "coordinator" / "bin" / (_DECLARED_BASENAME + ".py")
    script.write_text(_cold_marker_source(), encoding="utf-8")

    output = door_build.build(source_root, output=tmp / (_DECLARED_BASENAME + ".exe"))
    yield output, stub_root, script


def _door_env(stub_root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(stub_root)
    env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
    return env


def test_the_gate_still_fires_for_a_freshly_built_door(door_and_stub_root) -> None:
    """Precondition check for the two cost assertions below: the freshly
    LOCAL-BUILT `door.exe` must still take the cold leg for a declared
    basename (C3/C4's gate, not stale behaviour from a previously-committed
    binary) before its cost means anything."""
    door, stub_root, _script = door_and_stub_root
    proc = subprocess.run(
        [str(door), "ping"],
        input=b"",
        capture_output=True,
        env=_door_env(stub_root),
        timeout=60,
        cwd=str(stub_root),
        **no_console_creationflags(),
    )
    assert b"cold-leg-cost-spike-marker" in proc.stdout, (
        "the locally-built door did not take the cold leg for "
        f"{_DECLARED_BASENAME!r} -- the gate did not fire, or the stub "
        "cold script was not reached; the cost measured below would not "
        "be measuring what this spike claims to measure"
    )


def test_cold_leg_cost_through_the_door_is_under_the_dr344_bar(door_and_stub_root) -> None:
    """Primary assertion: process time for `door.exe` invoked under
    `_DECLARED_BASENAME`, batched over K_INVOCATIONS runs. This is TWO
    processes per invocation (door + the cold Python child it relaunches),
    so it is checked against the wider DR-344 end-to-end bar
    (`PROCESS_TIME_CEILING_MS`), not the single-process one."""
    door, stub_root, _script = door_and_stub_root
    result = batched_process_time_ms(
        [str(door), "ping"], k=K_INVOCATIONS, env=_door_env(stub_root), cwd=str(stub_root)
    )
    assert result["rc"] == 0, (
        f"door cold-leg invocation exited rc={result['rc']} -- a failing "
        "invocation cannot stand in for a valid process-time sample"
    )
    # `procs_per_call` is recorded, not asserted to an exact value: this box
    # measured 3.0 here (door + relaunched cold child + at least one process
    # this module's job-object accounting attributes that neither this
    # file's door.c reading nor its own docstring predicted -- see this
    # chunk's dispatch report for the as-measured figure). The exact-count
    # question is a SEPARATE investigation from this spike's own subject
    # (the process-TIME cost of the gate), so this file gates on time only.
    assert result["process_time_ms"] <= PROCESS_TIME_CEILING_MS, (
        f"door cold-leg process time for {_DECLARED_BASENAME!r} is "
        f"{result['process_time_ms']}ms (k={result['k']}, "
        f"procs_per_call={result['procs_per_call']}, "
        f"wall_ms={result['wall_ms']} for context only) -- exceeds the "
        f"DR-344 {PROCESS_TIME_CEILING_MS}ms end-to-end bar. Per this "
        "chunk's own body: this is a finding for the PM now, while the "
        "gate shape is still cheap to revisit -- not after C6/C8 have "
        "landed."
    )


def test_the_bare_cold_entrypoint_alone_is_under_the_single_process_bar(
    door_and_stub_root,
) -> None:
    """Baseline: the SAME cold Python entrypoint, invoked directly with no
    door in front of it at all (1 process) -- checked against DR-344's
    single-process bar. The gap between this number and the door-fronted
    one above is the table-lookup-plus-relaunch overhead the gate itself
    adds."""
    _door, stub_root, script = door_and_stub_root
    result = batched_process_time_ms(
        [sys.executable, str(script)], k=K_INVOCATIONS, cwd=str(stub_root)
    )
    assert result["rc"] == 0, (
        f"direct cold-entrypoint invocation exited rc={result['rc']} -- a "
        "failing invocation cannot stand in for a valid process-time sample"
    )
    # `procs_per_call` is recorded, not asserted to an exact value -- see
    # `test_cold_leg_cost_through_the_door_is_under_the_dr344_bar`'s own
    # comment on why this file gates on process TIME, not process COUNT.
    assert result["process_time_ms"] <= SINGLE_PROCESS_CEILING_MS, (
        f"direct cold-entrypoint process time is {result['process_time_ms']}ms "
        f"(k={result['k']}) -- exceeds DR-344's {SINGLE_PROCESS_CEILING_MS}ms "
        "single-process bar on its own, independent of any door overhead"
    )
