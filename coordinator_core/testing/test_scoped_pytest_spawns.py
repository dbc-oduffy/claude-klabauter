"""Tests for the scoped_pytest launcher: allow-list, admission, contained tree, parent-death kill."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import SimpleNamespace

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[2]
_GIB = 1 << 30


@pytest.fixture(autouse=True)
def _top_level_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient depth is whatever launched this suite (scoped-pytest makes it 1); pin it to 0 so
    the launcher under test sees a top-level caller."""
    monkeypatch.setenv("COORDINATOR_PYTEST_DEPTH", "0")


@pytest.mark.spawns_process
@pytest.mark.parametrize("body,rc", [("assert False, 'MARKER_FAIL'", 1), ("assert True", 0)])
def test_child_pytest_output_reaches_redirected_files(tmp_path: Path, body, rc) -> None:
    (tmp_path / "test_x.py").write_text("def test_x():\n    %s\n" % body)
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.testing.scoped_pytest",
             "--allow", "test_x.py", "--", "test_x.py", "-q"],
            cwd=tmp_path, stdout=fo, stderr=fe, timeout=120,
            env={**os.environ, "PYTHONPATH": str(_REPO)},
            **no_console_creationflags(),
        )
    text = out.read_text(errors="replace") + err.read_text(errors="replace")
    assert "1 " in text and ("passed" in text or "failed" in text), text
    assert ("MARKER_FAIL" in text) == (rc == 1)
    assert proc.returncode == rc


_LAUNCHER_DRIVER = """
import contextlib, sys, types
from coordinator_core.testing import scoped_pytest as sp
sp._SHELL_NAMES = sp._SHELL_NAMES | {"python", "python3", "pythonw"}
G = 1 << 30
from coordinator_core.bash_guards import _heavy_command_class as hcc
st = hcc._load_box_memory_host().MemoryStatus(avail_phys=99 * G, total_phys=100 * G, commit_used=0, commit_limit=1 << 50)
host = types.SimpleNamespace(
    GIB=G, admission_file=lambda: "adm", read_recorded_peak=lambda p, k: None,
    read_memory_status=lambda: st, reserved_bytes=lambda p: 0, guard_floor_bytes=lambda: 0,
    admit=lambda peak, s, floor=0: types.SimpleNamespace(admit=True, need_avail=peak),
    admission_lock=lambda p: contextlib.nullcontext(), register_command=lambda *a: None)
sys.exit(sp.main(sys.argv[1:], host=host))
"""

_WRAPPER = """
import subprocess, sys
sys.exit(subprocess.Popen([sys.executable] + sys.argv[1:]).wait())
"""
