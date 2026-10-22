"""Tests for box-launch-windowless.py using a fake Popen; nothing real is spawned."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "box_launch_windowless_under_test", _BIN_DIR / "box-launch-windowless.py"
)
assert _spec is not None and _spec.loader is not None
bl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bl
_spec.loader.exec_module(bl)


class FakePopen:
    calls: list = []

    def __init__(self, cmd, **kwargs):
        self.pid = 4242
        FakePopen.calls.append((cmd, kwargs))


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.calls = []


def test_win32_flags_and_pythonw_swap(tmp_path, capsys):
    log = tmp_path / "x.log"
    rc = bl.main(
        ["--log", str(log), "--", str(tmp_path / "py" / "pythonw.exe"), "job.py"],
        popen=FakePopen,
        platform="win32",
    )
    assert rc == 0
    cmd, kw = FakePopen.calls[0]
    assert Path(cmd[0]).name == "python.exe"
    assert kw["creationflags"] & 0x08000000
    assert kw["creationflags"] & 0x00000200
    assert not kw["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert "start_new_session" not in kw
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] is kw["stderr"]
    assert capsys.readouterr().out.strip() == f"LAUNCHED pid=4242 log={log}"


def test_win32_single_call_no_breakaway(tmp_path):
    rc = bl.main(["--log", str(tmp_path / "l"), "--", "job.exe"], popen=FakePopen, platform="win32")
    assert rc == 0
    assert len(FakePopen.calls) == 1
    assert not FakePopen.calls[0][1]["creationflags"] & 0x01000000


def test_win32_oserror_is_one_line_exit_1(tmp_path, capsys):
    def boom(cmd, **kw):
        raise OSError("nope")

    rc = bl.main(["--log", str(tmp_path / "l"), "--", "job.exe"], popen=boom, platform="win32")
    assert rc == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1




def test_posix_new_session(tmp_path):
    rc = bl.main(["--log", str(tmp_path / "l"), "--", "echo", "hi"], popen=FakePopen, platform="linux")
    assert rc == 0
    cmd, kw = FakePopen.calls[0]
    assert cmd == ["echo", "hi"]
    assert kw["start_new_session"] is True
    assert "creationflags" not in kw
    assert kw["stdin"] == subprocess.DEVNULL


def test_default_log_under_git_toplevel_of_cwd(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    rc = bl.main(["--cwd", str(sub), "--", "tool.exe"], popen=FakePopen, platform="linux")
    assert rc == 0
    out = capsys.readouterr().out
    log = Path(out.split("log=", 1)[1].strip())
    assert log.parent == tmp_path.resolve() / "scratch"
    assert log.name.startswith("launch-tool-")
    assert FakePopen.calls[0][1]["cwd"] == str(sub)


def test_default_log_outside_git_uses_settings_home(tmp_path, monkeypatch):
    home = tmp_path / "sh"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    if bl._git_toplevel(work) is not None:
        pytest.skip("tmp_path sits inside a git tree")
    p = bl.default_log_path("x", work)
    assert p.parent == home / "scratch"


def test_launch_failure_is_one_line(tmp_path, capsys):
    def boom(cmd, **kw):
        raise FileNotFoundError("nope")

    rc = bl.main(["--log", str(tmp_path / "l"), "--", "missing"], popen=boom, platform="linux")
    assert rc == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "launch failed" in err[0]


@pytest.mark.parametrize("argv", [[], ["--log", "x"], ["--"], ["--bogus", "--", "a"]])
def test_usage_errors(argv, capsys):
    assert bl.main(argv, popen=FakePopen, platform="linux") == 2
    assert not FakePopen.calls


def test_help_exits_zero_on_linux(capsys):
    assert bl.main(["--help"], popen=FakePopen, platform="linux") == 0
    assert "usage" in capsys.readouterr().out
