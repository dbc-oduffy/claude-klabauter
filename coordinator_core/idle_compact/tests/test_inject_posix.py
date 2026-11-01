"""PosixInjector: ioctl call order, separate Enter, kernel-refusal skip (fcntl/os mocked)."""

from __future__ import annotations

import errno

import pytest

from coordinator_core.idle_compact import inject_posix
from coordinator_core.idle_compact.contract import InjectResult, Injector


class FakeFcntl:
    def __init__(self, fail_errno=None):
        self.calls = []
        self.fail_errno = fail_errno

    def ioctl(self, fd, request, arg):
        if self.fail_errno is not None:
            raise OSError(self.fail_errno, "refused")
        self.calls.append((fd, request, arg))


@pytest.fixture
def env(monkeypatch):
    events = []
    fake = FakeFcntl()
    monkeypatch.setattr(inject_posix, "fcntl", fake)
    monkeypatch.setattr(inject_posix, "resolve_tty", lambda pid: "/dev/pts/7")
    monkeypatch.setattr(inject_posix.os, "open", lambda path, flags: events.append(("open", path)) or 99)
    monkeypatch.setattr(inject_posix.os, "close", lambda fd: events.append(("close", fd)))
    return fake, events


def test_types_command_then_enter_after_delay(env):
    fake, events = env
    sleeps = []
    result = inject_posix.PosixInjector(sleep=sleeps.append).inject(123)
    assert result == InjectResult(True, "injected")
    assert [c[2] for c in fake.calls] == [c.encode() for c in "/compact"] + [b"\r"]
    assert sleeps == [0.5]
    assert events == [("open", "/dev/pts/7"), ("close", 99)]


@pytest.mark.parametrize("code", [errno.EPERM, errno.EIO])
def test_kernel_refusal_is_injector_unavailable(env, monkeypatch, code):
    fake, events = env
    fake.fail_errno = code
    result = inject_posix.PosixInjector(sleep=lambda s: None).inject(123)
    assert result == InjectResult(False, "injector-unavailable")
    assert ("close", 99) in events


def test_other_oserror_is_not_unavailable(env):
    fake, _ = env
    fake.fail_errno = errno.ENOMEM
    result = inject_posix.PosixInjector(sleep=lambda s: None).inject(123)
    assert result.ok is False and result.reason != "injector-unavailable"


def test_unresolved_tty_types_nothing(env, monkeypatch):
    fake, _ = env
    monkeypatch.setattr(inject_posix, "resolve_tty", lambda pid: None)
    result = inject_posix.PosixInjector(sleep=lambda s: None).inject(123)
    assert result == InjectResult(False, "tty-unresolved")
    assert fake.calls == []


def test_no_fcntl_is_unavailable(monkeypatch):
    monkeypatch.setattr(inject_posix, "fcntl", None)
    assert inject_posix.PosixInjector().inject(1) == InjectResult(False, "injector-unavailable")


def test_resolve_tty_linux_readlink(monkeypatch):
    monkeypatch.setattr(inject_posix.sys, "platform", "linux")
    monkeypatch.setattr(inject_posix.os, "readlink", lambda p: "/dev/pts/3" if p == "/proc/5/fd/0" else "pipe:[1]")
    assert inject_posix.resolve_tty(5) == "/dev/pts/3"
    monkeypatch.setattr(inject_posix.os, "readlink", lambda p: "pipe:[1]")
    assert inject_posix.resolve_tty(5) is None


def test_resolve_tty_darwin_psutil(monkeypatch):
    import sys
    import types

    class Proc:
        def __init__(self, pid):
            pass

        def terminal(self):
            return "/dev/ttys004"

    monkeypatch.setattr(inject_posix.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=Proc))
    assert inject_posix.resolve_tty(5) == "/dev/ttys004"


def test_satisfies_injector_protocol():
    assert isinstance(inject_posix.PosixInjector(), Injector)
