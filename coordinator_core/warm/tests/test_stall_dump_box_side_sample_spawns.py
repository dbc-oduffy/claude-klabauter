"""The full warm stall record carries a bounded box-side sample; the stub record and a failing sampler do not lose the dump."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from coordinator_core.warm import box_sample

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

WINDOWS = sys.platform == "win32"


@pytest.fixture(autouse=True)
def _fresh_sample_key():
    """An overrun test leaves `box-sample` stalled in `bounded_io` until its worker returns; start each test clean."""
    from coordinator_core import bounded_io

    with bounded_io._lock:
        bounded_io._stalled.pop("box-sample", None)
        bounded_io._inflight.pop("box-sample", None)


@pytest.mark.skipif(not WINDOWS, reason="live sample reads the Windows process table")
def test_sampler_sees_a_reader_in_the_top_list_and_defender_cpu_shape(tmp_path):
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"x" * (64 << 20))
    reader = subprocess.Popen(
        [sys.executable, "-c", f"import sys\nwhile True:\n    open(r'{blob}', 'rb').read()"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        time.sleep(0.3)
        text = box_sample.sample_lines(0.6)
    finally:
        reader.kill()
        reader.wait()
    lines = dict(line.split(": ", 1) for line in text.strip().split("\n"))
    assert f"pid={reader.pid} " in lines[f"box-io-read-top{box_sample.TOP_N}"]
    cpu = lines["box-msmpeng-cpu-pct"]
    assert cpu == "absent" or float(cpu.split(" ", 1)[0]) >= 0
