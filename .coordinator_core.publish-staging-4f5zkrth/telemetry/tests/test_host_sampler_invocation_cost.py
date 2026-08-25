"""
coordinator_core.telemetry.tests.test_host_sampler_invocation_cost -- the
FULL process-spawn-to-exit invocation-cost ratchet for
coordinator_core.telemetry.host_sampler.

Purpose: coordinator_core.telemetry.tests.test_host_sampler's
``test_sample_cost_ratchet`` bounds only the IN-PROCESS collection slice
(``_MAX_SAMPLE_COST_MS``) -- it cannot see package-import cost, because
pytest has already imported the package by the time that test runs. The
number a scheduler actually pays is the full ``python <path>
host_sampler.py`` wall clock, which is what regressed this module's own
cost in the first place (see host_sampler.py's module docstring
"Invocation-cost ratchet"). This file is kept SEPARATE from
test_host_sampler.py (rather than adding a spawning test there) so the
existing fast, non-spawning tests in that file are not pulled onto the
cadence tier by SPAWN-RATCHET Rule 4, which requires module-level
``pytest.mark.cadence`` for any file containing so much as one spawning
test.

Spec backlink: state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md
               coordinator_core/telemetry/host_sampler.py (module docstring,
               "Invocation-cost ratchet")
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.telemetry import host_sampler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SAMPLER_SCRIPT = Path(host_sampler.__file__).resolve()


def test_direct_script_invocation_avoids_package_init(tmp_path: Path) -> None:
    """The deployed invocation path (``python <path>\\host_sampler.py``)
    must never execute ``coordinator_core/__init__.py`` -- that is the
    entire point of the fix. Verified by running the script in a fresh
    subprocess and asking it to report whether ``coordinator_core`` ever
    landed in ``sys.modules``."""
    (tmp_path / ".git").mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import runpy, sys\n"
        f"runpy.run_path(r'{_SAMPLER_SCRIPT}', run_name='__main__')\n"
        "print('PKG_IMPORTED=' + str('coordinator_core' in sys.modules))\n",
        encoding="utf-8",
    )
    env = {"COORDINATOR_HOST_SAMPLER_DISABLE": "1"}
    import os

    full_env = dict(os.environ)
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    assert "PKG_IMPORTED=False" in result.stdout, (
        "host_sampler.py's direct-script invocation imported the "
        "coordinator_core package -- the import-cost fix regressed:\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_end_to_end_invocation_cost_ratchet(tmp_path: Path) -> None:
    """Enforcement half of the FULL invocation-cost budget -- see
    host_sampler.py's ``_MAX_INVOCATION_COST_MS`` for the derivation. Takes
    the min of several spawns as the noise floor (this box runs 50-70
    concurrent sessions; subprocess wall-clock timing is noisy under that
    load -- see that constant's own comment).

    Every spawn sets ``COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE`` to a
    tmp_path-local file (see host_sampler.py's "Sink override" / "Measuring
    the sampler must not pollute what it measures") -- this measures
    process-spawn-to-exit cost, not sink placement, and must never land a
    row in any real, git-resolved sink (this repo's included) purely because
    the benchmark ran from a checkout on disk."""
    (tmp_path / ".git").mkdir()
    import os
    import time

    sink_override = tmp_path / "benchmark-host-samples.jsonl"
    env = dict(os.environ)
    env["COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE"] = str(sink_override)

    times_ms = []
    for _ in range(5):
        t0 = time.perf_counter()
        result = subprocess.run(
            [sys.executable, str(_SAMPLER_SCRIPT)],
            cwd=str(tmp_path),
            capture_output=True,
            env=env,
            timeout=30,
            **no_console_creationflags(),
        )
        times_ms.append((time.perf_counter() - t0) * 1000.0)
        assert result.returncode == 0, result.stderr

    floor_ms = min(times_ms)
    assert floor_ms <= host_sampler._MAX_INVOCATION_COST_MS, (
        f"host_sampler.py full invocation floor {floor_ms:.1f}ms exceeds its "
        f"{host_sampler._MAX_INVOCATION_COST_MS}ms high-water mark (samples: "
        f"{[round(t, 1) for t in times_ms]}) -- shrink the invocation cost, "
        "don't raise the ratchet."
    )
