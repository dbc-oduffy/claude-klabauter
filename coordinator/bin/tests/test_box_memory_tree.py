"""Tests for coordinator/bin/box-memory-tree.py with a fake process table; no real signals."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL = Path(__file__).resolve().parents[1] / "box-memory-tree.py"
_spec = importlib.util.spec_from_file_location("box_memory_tree", TOOL)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

GOVERNOR = Path(__file__).resolve().parents[1] / "group-em-memory-governor.py"


def _proc(pid, ppid, cmd="worker"):
    return SimpleNamespace(pid=pid, ppid=ppid, cmd=cmd, age_s=0.0, rss_mb=0.0)


class FakeGovernor:
    def __init__(self, procs):
        self.procs = procs
        self.signalled = []

    Proc = staticmethod(lambda pid, ppid, age_s, rss_mb, cmd: _proc(pid, ppid, cmd))

    def list_procs(self):
        return self.procs

    def descendants(self, root, procs):
        out, stack = [], [root]
        while stack:
            pid = stack.pop()
            out.append(pid)
            stack.extend(p.pid for p in procs if p.ppid == pid)
        return out

    def is_protected(self, p):
        return "claude" in p.cmd

    def signal_tree(self, pids, stop):
        self.signalled.append((sorted(pids), stop))


def test_suspend_tree(capsys):
    gov = FakeGovernor([_proc(1, 0), _proc(2, 1), _proc(3, 2)])
    assert mod.main(["suspend", "1"], governor=gov, platform="linux") == 0
    assert capsys.readouterr().out == "SUSPENDED pid=1 tree=3 skipped=0\n"
    assert gov.signalled == [([1, 2, 3], True)]


def test_resume_tree(capsys):
    gov = FakeGovernor([_proc(1, 0), _proc(2, 1)])
    assert mod.main(["resume", "1"], governor=gov, platform="linux") == 0
    assert capsys.readouterr().out == "RESUMED pid=1 tree=2 skipped=0\n"
    assert gov.signalled == [([1, 2], False)]


def test_protected_root_refuses(capsys):
    gov = FakeGovernor([_proc(1, 0, "claude"), _proc(2, 1)])
    assert mod.main(["suspend", "1"], governor=gov, platform="linux") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert gov.signalled == []


def test_protected_descendant_skipped(capsys):
    gov = FakeGovernor([_proc(1, 0), _proc(2, 1, "claude"), _proc(3, 1)])
    assert mod.main(["suspend", "1"], governor=gov, platform="linux") == 0
    assert capsys.readouterr().out == "SUSPENDED pid=1 tree=2 skipped=1\n"
    assert gov.signalled == [([1, 3], True)]


def test_missing_root_exits_1(capsys):
    gov = FakeGovernor([_proc(1, 0)])
    assert mod.main(["suspend", "99"], governor=gov, platform="linux") == 1
    assert "no such process" in capsys.readouterr().err


def test_posix_run_uses_real_governor_signal_branch(monkeypatch, capsys):
    gov = mod._load_host().load_governor(GOVERNOR)
    sent = []
    monkeypatch.setattr(gov, "psutil", None)
    # Windows' signal module has no SIGSTOP/SIGCONT; stand-ins keep the POSIX branch testable there.
    monkeypatch.setattr(gov, "signal", SimpleNamespace(SIGSTOP=19, SIGCONT=18))
    monkeypatch.setattr(gov.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(gov, "list_procs", lambda: [_proc(5, 0, "w"), _proc(6, 5, "w")])
    assert mod.main(["suspend", "5"], governor=gov, platform="linux") == 0
    assert sent == [(5, gov.signal.SIGSTOP), (6, gov.signal.SIGSTOP)]
    assert mod.main(["resume", "5"], governor=gov, platform="linux") == 0
    assert sent[2:] == [(5, gov.signal.SIGCONT), (6, gov.signal.SIGCONT)]
    capsys.readouterr()


def test_missing_psutil_exits_1_one_line(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert mod.main(["suspend", "1"], governor=FakeGovernor([]), platform="win32") == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1 and "psutil" in captured.err


def test_help_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as exc:
        mod.main(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()


def _row(pid, ppid):
    return SimpleNamespace(pid=pid, ppid=ppid, create_time=0.0, working_set_bytes=0)


def _win(argv, rows, cmds, gov=None):
    gov = gov or FakeGovernor([])
    asked = []

    def cmdline(pid):
        asked.append(pid)
        return cmds.get(pid, "worker")

    gov.list_procs = lambda: pytest.fail("win32 must not use list_procs")
    rc = mod.main(argv, governor=gov, platform="win32",
                  proc_table_fn=lambda: rows, cmdline_fn=cmdline)
    return rc, gov, asked


def test_win32_suspend_tree_reads_cmdline_for_tree_only(capsys):
    rows = [_row(1, 0), _row(2, 1), _row(3, 2), _row(9, 0), _row(10, 9)]
    rc, gov, asked = _win(["suspend", "1"], rows, {})
    assert rc == 0
    assert capsys.readouterr().out == "SUSPENDED pid=1 tree=3 skipped=0\n"
    assert gov.signalled == [([1, 2, 3], True)]
    assert sorted(asked) == [1, 2, 3]


def test_win32_protected_root_refuses(capsys):
    rc, gov, _ = _win(["suspend", "1"], [_row(1, 0), _row(2, 1)], {1: "claude"})
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("\n") == 1
    assert gov.signalled == []


def test_win32_protected_descendant_skipped(capsys):
    rc, gov, _ = _win(["suspend", "1"], [_row(1, 0), _row(2, 1), _row(3, 1)], {2: "claude"})
    assert rc == 0
    assert capsys.readouterr().out == "SUSPENDED pid=1 tree=2 skipped=1\n"
    assert gov.signalled == [([1, 3], True)]


def test_win32_missing_root_exits_1(capsys):
    rc, gov, asked = _win(["resume", "99"], [_row(1, 0)], {})
    assert rc == 1 and "no such process" in capsys.readouterr().err
    assert asked == [] and gov.signalled == []


def test_no_copied_tree_logic_and_no_kill():
    text = TOOL.read_text(encoding="utf-8")
    assert text.count("def descendants") == 0
    assert "SIGKILL" not in text and ".kill(" not in text and ".terminate(" not in text
