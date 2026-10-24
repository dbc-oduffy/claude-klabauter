"""Windows process-time gate: box-memory-snapshot and box-memory-tree each cost under 500ms CPU."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows process-time gate"),
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

BIN = Path(__file__).resolve().parent.parent
SNAPSHOT = BIN / "box-memory-snapshot.py"
TREE = BIN / "box-memory-tree.py"
BUDGET_S = 0.5

_CHILD = "import time; time.sleep(120)"
_PARENT = (
    "import subprocess, sys, time\n"
    f"c = subprocess.Popen([sys.executable, '-c', {_CHILD!r}])\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def _cpu_seconds(proc: subprocess.Popen) -> float:
    """Kernel+user time of an exited child, read from its Popen handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(
        wintypes.HANDLE(int(proc._handle)),  # type: ignore[attr-defined]
        ctypes.byref(creation),
        ctypes.byref(exit_),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    assert ok, f"GetProcessTimes failed: {ctypes.get_last_error()}"

    def ticks(ft) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    return (ticks(kernel) + ticks(user)) / 1e7


def _run_cli(script: Path, *args: str, ok_codes=(0,)) -> float:
    proc = subprocess.Popen(
        [sys.executable, str(script), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out, err = proc.communicate(timeout=120)
    assert proc.returncode in ok_codes, (
        f"{script.name} {args} exit {proc.returncode}\n"
        f"stdout: {out.decode(errors='replace')}\nstderr: {err.decode(errors='replace')}"
    )
    return _cpu_seconds(proc)


def test_snapshot_plain_under_budget():
    assert _run_cli(SNAPSHOT) < BUDGET_S


def test_snapshot_json_under_budget():
    assert _run_cli(SNAPSHOT, "--json") < BUDGET_S


def test_snapshot_admit_peak_under_budget(tmp_path):
    cost = _run_cli(
        SNAPSHOT,
        "--admit-peak",
        "1",
        "--admission-file",
        str(tmp_path / "admission.jsonl"),
        ok_codes=(0, 4),
    )
    assert cost < BUDGET_S


def test_tree_suspend_resume_under_budget():
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT],
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline().decode().strip())
        time.sleep(0.2)
        costs = {}
        try:
            costs["suspend"] = _run_cli(TREE, "suspend", str(parent.pid))
        finally:
            costs["resume"] = _run_cli(TREE, "resume", str(parent.pid))
        for action, cost in costs.items():
            assert cost < BUDGET_S, f"{action} cost {cost:.3f}s"
    finally:
        parent.kill()
        parent.wait()
        if child_pid is not None:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(child_pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
