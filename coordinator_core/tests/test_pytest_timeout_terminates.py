"""A CPU-bound test must be ended by `--timeout` on every platform, plain and under xdist.

On Windows pytest-timeout has no SIGALRM, so the only method is `thread`, which
`os._exit`s the process; xdist reports that as a crashed worker. Either way the
run must end near the limit rather than spin until an operator kills it.
"""

import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_LIMIT_S = 2
_CEILING_S = 60

_SPIN = "def test_spin():\n    while True:\n        sum(range(1000))\n"


@pytest.mark.deliberate_wall_clock(reason="the assertion IS that a hung run ends in real time; process time of a killed child is not observable")
@pytest.mark.parametrize("extra", [[], ["-n", "1"]], ids=["plain", "xdist"])
def test_timeout_ends_a_cpu_bound_test(tmp_path, extra):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_spin.py").write_text(_SPIN)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_spin.py", f"--timeout={_LIMIT_S}",
         "-p", "no:randomly", "-p", "no:cacheprovider", "-q", *extra],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=_CEILING_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    elapsed = time.monotonic() - started
    assert proc.returncode != 0, proc.stdout[-500:]
    assert elapsed < _CEILING_S, f"timeout did not end the run ({elapsed:.0f}s)"
