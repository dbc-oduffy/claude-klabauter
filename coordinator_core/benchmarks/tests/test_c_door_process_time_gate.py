"""K=20 batched process-time gate for the C door binary this plan names
(`warm/door/door.exe` on Windows, an install-path-built `door_posix` on
Darwin) -- new instrument, distinct from `test_warm_door_process_time_gate.py`
which measures `python -m coordinator_core.invoke ping` and whose own
docstring puts the C door outside its subject (trap T-2,
docs/plans/2026-09-06-warm-engine-survival-and-door-measurement.md `id: T4`).

PLATFORM SCOPE OF THIS FILE'S OWN GATE -- Windows and Darwin only, never
Linux. `process_time.py :: batched_process_time_ms` now has THREE
implemented platform arms (`IS_WINDOWS`, `IS_DARWIN`, `IS_LINUX` -- Linux
added for this primitive, see that module's own docstring), so the
Linux-raises-NotImplementedError premise this file used to rest on no
longer holds for the primitive itself. This file's own K=20 door gate
still SKIPS on Linux, but for a narrower reason that survived the change:
no identity-stamped door binary is built or measured here on this run
(the door binary, not `batched_process_time_ms`, is this gate's actual
subject) -- never substitute a different quantity (wall clock, a single-
invocation `getrusage` read, or the Python-invoke harness's figure) in its
place, each of those is a different unit and the substitution is exactly
the false-PASS class `process_time.py`'s docstring enumerates.

This chunk (T4) was executed on Linux (`sys.platform == "linux"`), so the
K=20 door binary was never built or measured here: AC2 is NOT TAKEN on this
run, the platform gap is named in
`docs/research/warm-engine-premise/k20-door-and-worker-survival.md`, and
this file records that live probe as a pinning test (the platform-dispatch
boundary itself) rather than a gate against a number no Linux box can
produce. A Windows or Darwin run authors the K=20 door batch call and its
<50ms p50/p90 assertion against `PROCESS_TIME_CEILING_MS` in this same file
when it lands; this test does not invent that call ahead of a binary to
point it at.

NEGATIVE SPEC: does not call `batched_process_time_ms` against
`python -m coordinator_core.invoke ping` (that is
`test_warm_door_process_time_gate.py`'s subject) and does not assert
against the 200ms/120ms ceilings that file uses -- this plan's AC2 bar is
the brightline's own <50ms (CLAUDE.md § brightline, "Warm engine, <50ms to
reach it"), stated explicitly as a different target.
"""

from __future__ import annotations

import sys

import pytest

import coordinator_core.benchmarks.process_time as process_time
from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)


def test_batched_process_time_ms_unsupported_platform_raises_not_implemented(monkeypatch):
    """Pins the platform-dispatch fallback `batched_process_time_ms` depends
    on: Windows, Darwin, and Linux each have a primitive; a platform with
    none of the three still raises NotImplementedError. Forces the fallback
    via the module's own IS_WINDOWS/IS_DARWIN/IS_LINUX flags rather than
    relying on this box's actual platform lacking a primitive -- Linux
    used to be that platform (raising for real, unpatched), but gained a
    primitive (`_linux_batched_process_time_ms`), which is exactly what
    made the prior version of this test, asserting a real, unpatched raise
    on Linux, start failing. Patching all three flags off keeps this pin
    meaningful on every platform the three arms now cover instead of going
    vacuous (or false) the moment any one of them stops being the
    unimplemented case."""
    monkeypatch.setattr(process_time, "IS_WINDOWS", False)
    monkeypatch.setattr(process_time, "IS_DARWIN", False)
    monkeypatch.setattr(process_time, "IS_LINUX", False)
    with pytest.raises(NotImplementedError):
        batched_process_time_ms(["true"], k=1)


@pytest.mark.skipif(
    not (IS_WINDOWS or IS_DARWIN),
    reason=(
        "AC2 (K=20 batched process time, door binary, <50ms bar) NOT TAKEN "
        f"on this platform ({sys.platform}): no identity-stamped door binary "
        "is built or measured here this run (see docs/research/"
        "warm-engine-premise/k20-door-and-worker-survival.md). Never "
        "substitute wall clock, a single-invocation getrusage read, or the "
        "Python-invoke harness's figure for this gate."
    ),
)
def test_c_door_k20_batched_process_time_under_brightline_bar():
    pytest.skip(
        "Not authored on this run (Linux, sys.platform=%r). The K=20 door "
        "batch call and its <50ms assertion belong here on a Windows or "
        "Darwin run that has a door binary to point them at." % sys.platform
    )
