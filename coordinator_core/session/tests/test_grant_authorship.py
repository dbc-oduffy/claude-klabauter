"""Tests for coordinator_core.session.grant_authorship.

Covers: HUMAN on a POSIX climb that COMPLETES (reaches the top of the
process tree with no harness ancestor found, ``walk-miss:no-parent``),
AGENT on a harness ancestor at depth > 1 on both platform arms (the case
that was broken before the first fix — a single-parent name-mismatch check
used to read POSIX as HUMAN even with a harness ancestor one rung further
up), and one case per ``walk-miss:*`` reason asserting the
platform-specific disposition (Windows: every miss refuses; POSIX: only a
COMPLETED climb — ``no-parent`` — reaches HUMAN, an INCOMPLETE climb —
``depth-exhausted``, where a harness ancestor could still sit above the
cap — and every other miss shape refuses).
"""

from __future__ import annotations

import pytest

from coordinator_core.session import core as ga_core
from coordinator_core.session import grant_authorship as ga


class _FakeProc:
    def __init__(self, cmdline=None, name="", ppid=None):
        self._cmdline = cmdline or []
        self._name = name
        self._ppid = ppid

    def cmdline(self):
        return self._cmdline

    def name(self):
        return self._name

    def ppid(self):
        return self._ppid

    def create_time(self):
        return 123456.0


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _FakeZombieProcess(Exception):
    pass


class _FakeError(Exception):
    pass


class _FakePsutilModule:
    """Minimal stand-in for the psutil module, exposing the exception
    classes ``_find_windows_claude_ancestor`` catches and a ``Process``
    factory the test configures per-case."""

    NoSuchProcess = _FakeNoSuchProcess
    AccessDenied = _FakeAccessDenied
    ZombieProcess = _FakeZombieProcess
    Error = _FakeError

    def __init__(self, process_factory):
        self._process_factory = process_factory

    def Process(self, pid):
        return self._process_factory(pid)


def _install_fake_psutil(monkeypatch, process_factory):
    # ``_find_windows_claude_ancestor`` reads ``_psutil()`` off its OWN
    # module (core.py), not ga's imported alias — both must be patched so
    # ga's own psutil-absent guard and the walk it delegates to see the
    # same fake.
    fake = _FakePsutilModule(process_factory)
    monkeypatch.setattr(ga, "_psutil", lambda: fake)
    monkeypatch.setattr(ga_core, "_psutil", lambda: fake)


# ---------------------------------------------------------------------------
# HUMAN — POSIX only: a climb that COMPLETES (reaches the top of the
# process tree with no harness ancestor found).
# ---------------------------------------------------------------------------


def test_human_on_completed_posix_chain(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)

    # ppid() returns falsy -> the climb reaches the top of the process
    # tree (no-parent) without ever finding a harness ancestor. This is a
    # COMPLETED climb, the only clean HUMAN answer this mechanism has.
    _install_fake_psutil(
        monkeypatch, lambda pid: _FakeProc(cmdline=["/bin/bash"], name="bash", ppid=0)
    )

    result = ga.authorship_verdict(start_pid=4242)

    assert result.verdict is ga.Verdict.HUMAN
    assert result.reason == "walk-miss:no-parent"
    assert result.refuses is False


# ---------------------------------------------------------------------------
# AGENT — a harness ancestor found anywhere in the climb, either platform.
# This is the case that was broken before the fix: on POSIX, a harness
# ancestor beyond the immediate parent used to be invisible to the old
# single-parent name check, which returned HUMAN for it.
# ---------------------------------------------------------------------------


def test_agent_on_posix_harness_ancestor_beyond_immediate_parent(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)

    # Rung 0: a shell (the immediate parent — a name-mismatch under the old
    # single-parent check). Rung 1: the actual harness ancestor.
    procs = {
        100: _FakeProc(cmdline=["/bin/bash"], name="bash", ppid=101),
        101: _FakeProc(cmdline=["/usr/local/bin/claude", "--flag"], name="claude"),
    }
    _install_fake_psutil(monkeypatch, lambda pid: procs[pid])

    result = ga.authorship_verdict(start_pid=100)

    assert result.verdict is ga.Verdict.AGENT
    assert result.reason == "walk-hit:1"
    assert result.refuses is True


def test_agent_on_windows_harness_ancestor(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", True)
    monkeypatch.setattr(ga, "_psutil", lambda: _FakePsutilModule(lambda pid: _FakeProc()))
    monkeypatch.setattr(
        ga,
        "_find_windows_claude_ancestor",
        lambda start_pid, max_depth=None: ((start_pid, 123456.0), "walk-hit:2"),
    )

    result = ga.authorship_verdict(start_pid=555)

    assert result.verdict is ga.Verdict.AGENT
    assert result.reason == "walk-hit:2"
    assert result.refuses is True


# ---------------------------------------------------------------------------
# UNRESOLVED — Windows: every walk-miss reason refuses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "walk-miss:depth-exhausted",
        "walk-miss:no-parent",
        "walk-miss:rung-unreadable:NoSuchProcess:1",
    ],
)
def test_unresolved_on_every_windows_walk_miss_reason(monkeypatch, reason):
    monkeypatch.setattr(ga, "_IS_WINDOWS", True)
    monkeypatch.setattr(ga, "_psutil", lambda: _FakePsutilModule(lambda pid: _FakeProc()))
    monkeypatch.setattr(
        ga,
        "_find_windows_claude_ancestor",
        lambda start_pid, max_depth=None: (None, reason),
    )

    result = ga.authorship_verdict(start_pid=777)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == reason
    assert result.refuses is True


def test_unresolved_windows_psutil_absent(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", True)
    monkeypatch.setattr(ga, "_psutil", lambda: None)

    result = ga.authorship_verdict(start_pid=1)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:psutil-absent"
    assert result.refuses is True


def test_unresolved_windows_walk_raises_unexpected_exception(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", True)
    monkeypatch.setattr(ga, "_psutil", lambda: _FakePsutilModule(lambda pid: _FakeProc()))

    def _boom(start_pid, max_depth=None):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ga, "_find_windows_claude_ancestor", _boom)

    result = ga.authorship_verdict(start_pid=2)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:RuntimeError"
    assert result.refuses is True


# ---------------------------------------------------------------------------
# UNRESOLVED — POSIX: every walk-miss reason EXCEPT no-parent refuses.
# ---------------------------------------------------------------------------


def test_unresolved_posix_rung_unreadable(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)

    def _raise_no_such_process(pid):
        raise _FakeNoSuchProcess("gone")

    _install_fake_psutil(monkeypatch, _raise_no_such_process)

    result = ga.authorship_verdict(start_pid=3)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:rung-unreadable:_FakeNoSuchProcess:0"
    assert result.refuses is True


def test_unresolved_posix_depth_exhausted(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)

    # Every rung is a readable, non-"claude" process whose ppid chains to
    # the next rung — the climb hits its depth bound WITHOUT completing
    # (the chain is still going): incomplete, not clean, stays ambiguous.
    def _factory(pid):
        return _FakeProc(cmdline=["/bin/bash"], name="bash", ppid=pid + 1)

    _install_fake_psutil(monkeypatch, _factory)

    result = ga.authorship_verdict(start_pid=9)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:depth-exhausted"
    assert result.refuses is True


def test_unresolved_posix_psutil_absent(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    monkeypatch.setattr(ga, "_psutil", lambda: None)

    result = ga.authorship_verdict(start_pid=4)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:psutil-absent"
    assert result.refuses is True


def test_unresolved_posix_walk_raises_unexpected_exception(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    monkeypatch.setattr(ga, "_psutil", lambda: _FakePsutilModule(lambda pid: _FakeProc()))

    def _boom(start_pid, max_depth=None):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ga, "_find_windows_claude_ancestor", _boom)

    result = ga.authorship_verdict(start_pid=5)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:RuntimeError"
    assert result.refuses is True


# ---------------------------------------------------------------------------
# default start_pid
# ---------------------------------------------------------------------------


def test_default_start_pid_is_os_getppid(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    seen = {}

    def _record_and_miss(pid):
        seen["pid"] = pid
        return _FakeProc(cmdline=["/bin/bash"], name="bash", ppid=0)

    _install_fake_psutil(monkeypatch, _record_and_miss)

    import os

    ga.authorship_verdict()

    assert seen["pid"] == os.getppid()
