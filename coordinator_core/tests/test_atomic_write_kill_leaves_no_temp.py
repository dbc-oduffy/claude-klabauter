"""atomic_write_bytes leaves no temp behind a killed writer or an interrupted write."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import atomic_replace
from coordinator_core.atomic_replace import atomic_write_bytes

REPO = Path(__file__).resolve().parents[2]


def _temps(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.startswith(".atomic-write."))


def test_temp_name_carries_target_and_pid(tmp_path, monkeypatch):
    seen = []
    real = atomic_replace._replace_with_retry

    def spy(src, dst):
        seen.append(Path(src).name)
        real(src, dst)

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", spy)
    atomic_write_bytes(tmp_path / "out.txt", b"x")
    assert seen[0].startswith(f".atomic-write.out.txt.{os.getpid()}.")
    assert _temps(tmp_path) == []


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_base_exception_unlinks_temp(tmp_path, monkeypatch, exc):
    def boom(src, dst):
        raise exc()

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", boom)
    with pytest.raises(exc):
        atomic_write_bytes(tmp_path / "out.txt", b"x")
    assert _temps(tmp_path) == []
    assert not (tmp_path / "out.txt").exists()


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_next_write_sweeps_dead_pid_leftover_only(tmp_path):
    dead = tmp_path / f".atomic-write.out.txt.{_dead_pid()}.abc123"
    dead.write_bytes(b"leftover")
    live = tmp_path / f".atomic-write.out.txt.{os.getpid()}.zzz999"
    live.write_bytes(b"live")
    sibling = tmp_path / f".atomic-write.out.txt.bak.{_dead_pid()}.abc123"
    sibling.write_bytes(b"other target")
    atomic_replace._last_sweep.clear()
    atomic_write_bytes(tmp_path / "out.txt", b"new")
    assert not dead.exists()
    assert live.exists()
    assert sibling.exists()
    assert (tmp_path / "out.txt").read_bytes() == b"new"


@pytest.mark.parametrize("bound, swept", [(True, False), (False, True)])
def test_sweep_is_skipped_only_inside_the_submit_hook_budget(tmp_path, monkeypatch, bound, swept):
    import contextvars
    import types

    var = contextvars.ContextVar("submit_budget", default=None)
    fake = types.ModuleType("coordinator_core.hooks.submit_engine_ask")
    fake._BUDGET_VAR = var
    monkeypatch.setitem(sys.modules, "coordinator_core.hooks.submit_engine_ask", fake)
    dead = tmp_path / f".atomic-write.out.txt.{_dead_pid()}.abc123"
    dead.write_bytes(b"leftover")
    atomic_replace._last_sweep.clear()
    token = var.set([1.0]) if bound else None
    try:
        atomic_write_bytes(tmp_path / "out.txt", b"new")
    finally:
        if token is not None:
            var.reset(token)
    assert (not dead.exists()) is swept
    assert (tmp_path / "out.txt").read_bytes() == b"new"


def test_hard_killed_writer_leaves_no_temp_after_next_write(tmp_path):
    target = tmp_path / "out.txt"
    code = (
        "import os, sys, time\n"
        "from coordinator_core import atomic_replace\n"
        "def hang(src, dst):\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(60)\n"
        "atomic_replace._replace_with_retry = hang\n"
        f"atomic_replace.atomic_write_bytes({str(target)!r}, b'x')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], cwd=REPO, stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert proc.stdout.readline().strip() == b"ready"
        assert len(_temps(tmp_path)) == 1
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
    deadline = time.monotonic() + 5
    while atomic_replace._pid_alive(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    atomic_replace._last_sweep.clear()
    atomic_write_bytes(target, b"y")
    assert _temps(tmp_path) == []
    assert target.read_bytes() == b"y"
