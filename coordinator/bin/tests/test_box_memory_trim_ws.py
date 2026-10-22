"""Tests for coordinator/bin/box-memory-trim-ws.py with injected trim and reader; no real trim."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "box-memory-trim-ws.py"
_spec = importlib.util.spec_from_file_location("box_memory_trim_ws", TOOL)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_trimmed_line(capsys):
    sizes = iter([500, 20])
    rc = mod.main(["42"], platform="win32", trim=lambda p: None, reader=lambda p: next(sizes))
    assert rc == 0
    assert capsys.readouterr().out == "TRIMMED pid=42 wset=500MB->20MB\n"


def test_access_denied(capsys):
    def deny(pid):
        raise OSError(5, "Access is denied")

    rc = mod.main(["42"], platform="win32", trim=deny, reader=lambda p: 1)
    cap = capsys.readouterr()
    assert rc == 1 and cap.out == ""
    assert len(cap.err.strip().splitlines()) == 1


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_unsupported_host(plat, capsys):
    rc = mod.main(["42"], platform=plat)
    err = capsys.readouterr().err
    assert rc == 6
    assert len(err.strip().splitlines()) == 1 and "unsupported host" in err
    assert "Traceback" not in err


def test_help_exits_zero_before_platform_check(capsys):
    with pytest.raises(SystemExit) as exc:
        mod.main(["--help"], platform="linux")
    assert exc.value.code == 0
    assert "unsupported" not in capsys.readouterr().err
