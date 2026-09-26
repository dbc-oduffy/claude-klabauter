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

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)
from coordinator_core.telemetry import host_sampler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SAMPLER_SCRIPT = Path(host_sampler.__file__).resolve()


def _require_supported_platform() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "process-time accounting has no primitive for this platform -- "
            "see coordinator_core.benchmarks.process_time module docstring"
        )


def test_direct_script_invocation_avoids_package_init(tmp_path: Path) -> None:
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
    host_sampler.py's ``_MAX_INVOCATION_COST_MS`` for the derivation. Gates
    on PROCESS TIME (batched user+kernel CPU time via
    ``coordinator_core.benchmarks.process_time.batched_process_time_ms``),
    never wall clock -- wall clock on this box measures peer load (50-70
    concurrent sessions is the design condition), not cost (see that
    module's docstring and CLAUDE.md's "The brightline").

    Every spawn sets ``COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE`` to a
    tmp_path-local file (see host_sampler.py's "Sink override" / "Measuring
    the sampler must not pollute what it measures") -- this measures
    process-spawn-to-exit cost, not sink placement, and must never land a
    row in any real, git-resolved sink (this repo's included) purely because
    the benchmark ran from a checkout on disk."""
    _require_supported_platform()
    (tmp_path / ".git").mkdir()
    import os

    sink_override = tmp_path / "benchmark-host-samples.jsonl"
    env = dict(os.environ)
    env["COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE"] = str(sink_override)

    result = batched_process_time_ms(
        [sys.executable, str(_SAMPLER_SCRIPT)], k=10, env=env, cwd=str(tmp_path)
    )
    assert result["rc"] == 0

    process_time_ms = result["process_time_ms"]
    assert process_time_ms <= host_sampler._MAX_INVOCATION_COST_MS, (
        f"host_sampler.py full invocation process time {process_time_ms:.1f}ms "
        f"exceeds its {host_sampler._MAX_INVOCATION_COST_MS}ms high-water mark "
        f"(k={result['k']}) -- shrink the invocation cost, don't raise the "
        "ratchet."
    )

    floor_result = batched_process_time_ms(
        [sys.executable, "-c", "pass"], k=10, cwd=str(tmp_path)
    )
    assert result["procs_per_call"] <= floor_result["procs_per_call"], (
        f"host_sampler.py invocation spawned {result['procs_per_call']} "
        f"procs/call, exceeding the bare-interpreter floor of "
        f"{floor_result['procs_per_call']} -- the module's contract is no "
        "subprocess spawn anywhere."
    )
