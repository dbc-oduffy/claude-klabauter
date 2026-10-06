"""Atomic replace lands while another handle holds the target open (Windows)."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="WinError 5 is Windows-only")

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"


def _hold_then_release(target: Path, hold_s: float) -> threading.Thread:
    opened = threading.Event()

    def run() -> None:
        with open(target, "rb"):  # no FILE_SHARE_DELETE
            opened.set()
            time.sleep(hold_s)

    t = threading.Thread(target=run)
    t.start()
    opened.wait(5)
    return t


def test_machine_local_write_lands_under_a_held_reader(tmp_path):
    spec = importlib.util.spec_from_file_location("_ml_under_test", _IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    target = tmp_path / "registry.local.toml"
    target.write_text("old = 1\n", encoding="utf-8")
    t = _hold_then_release(target, 0.2)
    rc = mod._write_registry_file(str(target), "new = 2\n", is_new=False)
    t.join()
    assert rc == 0
    assert target.read_text(encoding="utf-8") == "new = 2\n"
    assert not list(tmp_path.glob("*.tmp.*"))


def test_atomic_write_bytes_lands_under_a_held_reader(tmp_path):
    from coordinator_core.atomic_replace import atomic_write_bytes

    target = tmp_path / "f.bin"
    target.write_bytes(b"old")
    t = _hold_then_release(target, 0.2)
    atomic_write_bytes(target, b"new")
    t.join()
    assert target.read_bytes() == b"new"
