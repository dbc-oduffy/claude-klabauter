"""`_machine_local.py` read verbs are bounded: declared keys never spawn or scan,
git stalls cannot outlast the deadline, and `dump` memory is O(keys)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_bounded_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)


@pytest.fixture
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "reg"
    d.mkdir()
    for k in [k for k in os.environ if k.startswith("REPO_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    monkeypatch.setattr(_ml, "_deadline_at", None)
    return d


@pytest.mark.deliberate_wall_clock(reason="the watchdog's wall-clock exit bound on a blocked read is the behaviour under test")
def test_watchdog_exits_with_named_error_when_a_read_blocks(tmp_path: Path) -> None:
    reg = tmp_path / "reg"
    reg.mkdir()
    code = (
        "import importlib.util,sys,time;"
        f"s=importlib.util.spec_from_file_location('m',r'{_IMPL}');m=importlib.util.module_from_spec(s);"
        "s.loader.exec_module(m);m._arm_deadline(0.3);time.sleep(30)"
    )
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert time.monotonic() - t0 < 5
    assert p.returncode == 4
    assert p.stderr.startswith("machine-local-timeout:") and "deadline of 0.3s exceeded" in p.stderr
