"""Tests for coordinator_core.session.grant_authorship.

Covers: HUMAN on a clean chain, AGENT on a harness ancestor (both platform
arms), and one case per Windows ``walk-miss:*`` reason asserting REFUSE
(UNRESOLVED) — plus the POSIX-side ambiguous misses, since that arm shares
the same fail-closed disposition via its own vocabulary.
"""

from __future__ import annotations

import pytest

from coordinator_core.session import grant_authorship as ga


class _FakeProc:
    def __init__(self, cmdline=None, name=""):
        self._cmdline = cmdline or []
        self._name = name

    def cmdline(self):
        return self._cmdline

    def name(self):
        return self._name


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
    classes ``_posix_parent_check`` catches and a ``Process`` factory the
    test configures per-case."""

    NoSuchProcess = _FakeNoSuchProcess
    AccessDenied = _FakeAccessDenied
    ZombieProcess = _FakeZombieProcess
    Error = _FakeError

    def __init__(self, process_factory):
        self._process_factory = process_factory

    def Process(self, pid):
        return self._process_factory(pid)


def _install_fake_psutil(monkeypatch, process_factory):
    monkeypatch.setattr(ga, "_psutil", lambda: _FakePsutilModule(process_factory))


# ---------------------------------------------------------------------------
# HUMAN — the one path this module can reach it from: a clean POSIX chain.
# ---------------------------------------------------------------------------


def test_human_on_clean_posix_chain(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    _install_fake_psutil(
        monkeypatch, lambda pid: _FakeProc(cmdline=["/bin/bash"], name="bash")
    )

    result = ga.authorship_verdict(start_pid=4242)

    assert result.verdict is ga.Verdict.HUMAN
    assert result.reason == "posix-parent-miss:name-mismatch"
    assert result.refuses is False


# ---------------------------------------------------------------------------
# AGENT — a harness ancestor found, on each platform arm.
# ---------------------------------------------------------------------------


def test_agent_on_posix_harness_ancestor(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    _install_fake_psutil(
        monkeypatch,
        lambda pid: _FakeProc(cmdline=["/usr/local/bin/claude", "--flag"], name="claude"),
    )

    result = ga.authorship_verdict(start_pid=100)

    assert result.verdict is ga.Verdict.AGENT
    assert result.reason == "posix-parent-hit"
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
# UNRESOLVED — one case per Windows walk-miss reason, asserting REFUSE.
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


def test_unresolved_posix_rung_unreadable(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)

    def _raise_no_such_process(pid):
        raise _FakeNoSuchProcess("gone")

    _install_fake_psutil(monkeypatch, _raise_no_such_process)

    result = ga.authorship_verdict(start_pid=3)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:rung-unreadable:_FakeNoSuchProcess"
    assert result.refuses is True


def test_unresolved_posix_psutil_absent(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    monkeypatch.setattr(ga, "_psutil", lambda: None)

    result = ga.authorship_verdict(start_pid=4)

    assert result.verdict is ga.Verdict.UNRESOLVED
    assert result.reason == "walk-miss:psutil-absent"
    assert result.refuses is True


# ---------------------------------------------------------------------------
# default start_pid
# ---------------------------------------------------------------------------


def test_default_start_pid_is_os_getppid(monkeypatch):
    monkeypatch.setattr(ga, "_IS_WINDOWS", False)
    seen = {}

    def _record_and_miss(pid):
        seen["pid"] = pid
        return _FakeProc(cmdline=["/bin/bash"], name="bash")

    _install_fake_psutil(monkeypatch, _record_and_miss)

    import os

    ga.authorship_verdict()

    assert seen["pid"] == os.getppid()
