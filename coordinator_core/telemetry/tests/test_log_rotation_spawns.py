
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.telemetry.log_rotation import rotate_if_needed

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_peer_holding_sink_open_never_holds_the_caller_past_the_bound(tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    sink = tmp_path / "x.jsonl"
    _write(sink, b"a" * 1000)
    code = (
        "import sys,time\n"
        "f = open(sys.argv[1], 'ab')\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(sink)],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert child.stdout.readline().strip() == b"ready"
        t0 = time.monotonic()
        result = rotate_if_needed(sink, threshold_bytes=1)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        if os.name == "nt":
            assert result is False
            assert sink.exists()
    finally:
        child.kill()
        child.wait()
