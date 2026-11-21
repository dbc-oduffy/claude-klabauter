"""orphan_reap: the dead-parent rule and the reap loop, against a fake census."""

import pytest

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.box_memory import orphan_reap

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _far_future_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.box_memory import pressure

    monkeypatch.setattr(pressure, "now_ctime", lambda: 10**15)


_UNIT = 10_000_000
_NOW = 10_000 * _UNIT


def test_real_live_pipeline_with_dead_parent_is_not_reaped_until_aged(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    import sys

    from coordinator_core.bash_guards import _host_probe
    from coordinator_core.bash_guards._host_probe import HostPrimitives

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    parent_code = (
        "import subprocess, sys;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        "print(c.pid, flush=True)"
    )
    out = subprocess.run([sys.executable, "-c", parent_code], capture_output=True, text=True,
                         timeout=30, creationflags=flags)
    child_pid = int(out.stdout.strip())
    # The orphaned child sleeps a bounded 20 s and exits itself; the harness refuses a kill outside the test tree.
    real = next((r for r in HostPrimitives.snapshot() or () if r.pid == child_pid), None)
    assert real is not None and real.ctime
    row = ProcRow(real.pid, real.ppid, real.ctime, "grep")

    class Prims:
        @staticmethod
        def creation_time(pid):
            # The exited parent's handle can linger in this process (Windows); report it gone.
            return None if pid == real.ppid else HostPrimitives.creation_time(pid)

        @staticmethod
        def snapshot() -> list[ProcRow]:
            return [row]

    per_s = _host_probe.ctime_units_per_second()
    killed = []
    young = row.ctime + 60 * per_s
    assert orphan_reap.reap(Prims, terminate=killed.append, self_pid=-1, now=young) == []
    assert killed == []
    aged = row.ctime + 31 * 60 * per_s
    assert orphan_reap.reap(Prims, terminate=killed.append, self_pid=-1, now=aged) == [child_pid]
