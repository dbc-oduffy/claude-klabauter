"""A booted_warm_engine teardown leaves no process rooted under its clone (read-only enumeration)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import isolated_clone as ic

_SETTLE_SECS = 5.0
_ENTRYPOINT = "box-memory-snapshot"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _rooted(tmp_parent: Path) -> list:
    import psutil

    root_norm = ic._normalized(tmp_parent)
    found = []
    for proc in psutil.process_iter():
        try:
            if ic._process_is_rooted_under(proc, root_norm):
                found.append(f"{proc.pid}:{' '.join(proc.cmdline() or ())[:160]}")
        except psutil.Error:
            continue
    return found


def _boot_and_tear_down(raise_inside: bool) -> list:
    source = _source_root()
    if not (source / "coordinator" / "bin" / f"{_ENTRYPOINT}.py").is_file():
        pytest.skip("no entrypoint script in source root")
    tmp_parent = None
    try:
        with ic.booted_warm_engine(
            source, prefix="clone-teardown-leak-", entrypoint=_ENTRYPOINT, label="teardown-leak"
        ) as (root, _pid, _env):
            tmp_parent = root.parent
            deadline = time.time() + _SETTLE_SECS
            while time.time() < deadline:
                time.sleep(0.5)
            if raise_inside:
                raise RuntimeError("test body failed")
    except RuntimeError as exc:
        if "test body failed" not in str(exc):
            raise
    except ic.WarmEngineBootTimeout:
        pytest.skip("isolated warm server did not boot")
    except ic.CloneTeardownLeak as exc:
        pytest.fail(f"teardown left a surviving process: {exc}")
    assert tmp_parent is not None
    return _rooted(tmp_parent)


def test_pass_path_leaves_no_rooted_process() -> None:
    assert _boot_and_tear_down(raise_inside=False) == []


def test_exception_path_leaves_no_rooted_process() -> None:
    assert _boot_and_tear_down(raise_inside=True) == []


_CHILD = """
import sys, time
from pathlib import Path
from coordinator_core.benchmarks import isolated_clone as ic
with ic.booted_warm_engine(Path(sys.argv[1]), prefix="clone-killed-parent-", entrypoint=sys.argv[2], label="killed") as (root, _p, _e):
    print(root.parent, flush=True)
    time.sleep(600)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="killed-parent path asserted on Windows")
def test_killed_parent_leaves_no_rooted_process() -> None:
    import psutil

    source = _source_root()
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(source), _ENTRYPOINT],
        cwd=str(source),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        line = child.stdout.readline().strip()
        if not line:
            pytest.skip("child engine did not boot")
        tmp_parent = Path(line)
        deadline = time.time() + 30
        while time.time() < deadline and not any("supervisor.py" in r for r in _rooted(tmp_parent)):
            time.sleep(0.5)
    finally:
        child.kill()
        child.wait(timeout=10)
    deadline = time.time() + 15
    left = _rooted(tmp_parent)
    while left and time.time() < deadline:
        time.sleep(0.5)
        left = _rooted(tmp_parent)
    assert left == [], f"processes survived a killed parent: {left}"
