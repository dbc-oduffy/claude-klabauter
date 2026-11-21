"""The conftest net refuses any test unlink under the live peer-socket dir."""

from __future__ import annotations

import os

import pytest
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_a_spawned_roster_reaps_only_the_redirected_dir(tmp_path: Path) -> None:
    """The sigterm test's shape: a real roster subprocess. It must see the tmp dir, never /tmp/cc-socks."""
    import subprocess
    import sys
    from pathlib import Path

    socks = os.environ["UNIMATRIX_LOOPBACK_SOCKS"]
    assert not socks.startswith(("/tmp/cc-socks", "/private/tmp/cc-socks"))
    roster = Path(__file__).resolve().parents[1] / "unimatrix-roster.py"
    probe = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('r',{str(roster)!r});m=importlib.util.module_from_spec(s);"
        "s.loader.exec_module(m);print(m.LOOPBACK_SOCKS)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip().splitlines()[-1] == socks, out.stderr
