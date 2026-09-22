"""K=20 batched process-time gate for the C door binary this plan names
(`warm/door/door.exe` on Windows, an install-path-built `door_posix` on
Darwin) -- new instrument, distinct from `test_warm_door_process_time_gate.py`
which measures `python -m coordinator_core.invoke ping` and whose own
docstring puts the C door outside its subject (trap T-2,
docs/plans/2026-09-06-warm-engine-survival-and-door-measurement.md `id: T4`).

PLATFORM SCOPE -- Windows and Darwin only, never Linux. `process_time.py ::
batched_process_time_ms` dispatches on exactly two platform arms
(`IS_WINDOWS`, `IS_DARWIN`) and raises `NotImplementedError` on every other
platform (see that module's docstring); this file's own K=20 door gate
therefore SKIPS on Linux rather than substituting a different quantity
(wall clock, a single-invocation `getrusage` read, or the Python-invoke
harness's figure) -- each of those is a different unit and the substitution
is exactly the false-PASS class `process_time.py`'s docstring enumerates.

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

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)


def test_batched_process_time_ms_platform_dispatch_has_no_linux_arm():
    """Pins the platform-dispatch boundary this file's own K=20 door gate
    depends on: exactly Windows and Darwin are implemented, Linux raises.
    This is what makes AC2 NOT TAKEN on a Linux run a fact about the
    instrument, not an oversight in this file."""
    if IS_WINDOWS or IS_DARWIN:
        pytest.skip(
            "process_time.py has a primitive on this platform; the K=20 "
            "door batch call belongs in a follow-on authored against a "
            "real door binary here, not in this dispatch-boundary pin."
        )
    with pytest.raises(NotImplementedError):
        batched_process_time_ms(["true"], k=1)


@pytest.mark.skipif(
    not (IS_WINDOWS or IS_DARWIN),
    reason=(
        "AC2 (K=20 batched process time, door binary, <50ms bar) NOT TAKEN "
        f"on this platform ({sys.platform}): batched_process_time_ms has no "
        "primitive here (see docs/research/warm-engine-premise/"
        "k20-door-and-worker-survival.md). Never substitute wall clock, a "
        "single-invocation getrusage read, or the Python-invoke harness's "
        "figure for this gate."
    ),
)
def test_c_door_k20_batched_process_time_under_brightline_bar():
    """Placeholder for the Windows/Darwin K=20 door batch gate. Authored
    against the identity-stamped door binary (`door.exe` via
    `door.image-identity.txt`/`door.exe.provenance.json` on Windows, an
    install-path-built `door_posix` on Darwin) by whichever T4 run lands on
    one of those platforms; this Linux run skips it rather than fabricate a
    number this box's instrument cannot produce."""
    pytest.skip(
        "Not authored on this run (Linux, sys.platform=%r). The K=20 door "
        "batch call and its <50ms assertion belong here on a Windows or "
        "Darwin run that has a door binary to point them at." % sys.platform
    )
