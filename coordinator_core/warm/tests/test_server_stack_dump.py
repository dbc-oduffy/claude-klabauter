"""coordinator_core/warm/tests/test_server_stack_dump.py — C5 of
`docs/plans/2026-09-20-stop-the-engine-spawning-to-talk-to-itself.md`.

Subject: `_register_stack_dump_signal`, the handler that lets an operator ask a
slow resident server what its threads are waiting on.

The properties worth pinning are not "does faulthandler work". They are the ones
that decide whether an operator on a bad day gets an answer: the dump reaches a
FILE (a detached server's stderr is `DEVNULL`), the registration never takes the
server down with it, and the no-signal platform SAYS SO instead of shipping a
handler that silently is not there.
"""

from __future__ import annotations

import signal

import pytest

from coordinator_core.warm import server as srv


_POSIX_ONLY = pytest.mark.skipif(
    getattr(signal, "SIGUSR1", None) is None,
    reason="SIGUSR1 does not exist on this platform — the absent branch is covered separately",
)


@pytest.fixture(autouse=True)
def _svc_dir_in_tmp(tmp_path, monkeypatch):
    """Autouse: a test that forgot would register a real signal handler in the
    test process and append to the operator's live svc dir."""
    monkeypatch.setattr(srv.breadcrumb, "svc_dir", lambda engine_root=None: tmp_path)
    yield tmp_path
    srv._STACK_DUMP_FILE = None


@_POSIX_ONLY
def test_the_dump_goes_to_a_file_beside_the_breadcrumb(tmp_path, monkeypatch):
    """A resident server is spawned detached with `stderr=DEVNULL`, so a dump to
    stderr is a dump into nothing. The file is the whole point."""
    registered = {}

    def _fake_register(sig, file=None, all_threads=False, chain=True):
        registered.update(sig=sig, file=file, all_threads=all_threads, chain=chain)

    monkeypatch.setattr(srv.faulthandler, "register", _fake_register)

    remedy = srv._register_stack_dump_signal()
    assert remedy is not None

    assert registered["sig"] == signal.SIGUSR1
    assert registered["all_threads"] is True
    assert registered["file"] is not None
    assert registered["file"].name == str(tmp_path / srv.STACK_DUMP_FILENAME)
    assert str(tmp_path / srv.STACK_DUMP_FILENAME) in remedy
    assert str(srv.os.getpid()) in remedy


@_POSIX_ONLY
def test_the_file_object_is_held_so_its_descriptor_cannot_be_collected(monkeypatch):
    """`faulthandler.register` keeps only the fd. If the file object it came from
    is garbage-collected the fd closes, and the handler then writes into a closed
    — or recycled — descriptor. Holding it at module scope is the fix, so the
    reference is the assertion."""
    monkeypatch.setattr(srv.faulthandler, "register", lambda *a, **kw: None)

    srv._register_stack_dump_signal()

    assert srv._STACK_DUMP_FILE is not None
    assert not srv._STACK_DUMP_FILE.closed


@_POSIX_ONLY
def test_the_dump_appends_rather_than_truncating(tmp_path, monkeypatch):
    """Several dumps over one server life must accumulate. An operator who takes
    a second sample to compare against the first must not destroy the first."""
    monkeypatch.setattr(srv.faulthandler, "register", lambda *a, **kw: None)
    target = tmp_path / srv.STACK_DUMP_FILENAME
    target.write_text("earlier dump\n", encoding="utf-8")

    srv._register_stack_dump_signal()
    srv._STACK_DUMP_FILE.write("later dump\n")
    srv._STACK_DUMP_FILE.flush()

    body = target.read_text(encoding="utf-8")
    assert "earlier dump" in body
    assert "later dump" in body


def test_the_absent_signal_branch_returns_none_rather_than_a_dead_handler(monkeypatch):
    """Windows has no SIGUSR1. Registering nothing and SAYING so is the
    contract: an operator who believes the box carries an instrument it does not
    is worse off than one told plainly that it does not."""
    monkeypatch.delattr(srv.signal, "SIGUSR1", raising=False)

    def _must_not_run(*a, **kw):  # pragma: no cover -- the assertion is that it never runs
        raise AssertionError("faulthandler.register reached on a platform with no SIGUSR1")

    monkeypatch.setattr(srv.faulthandler, "register", _must_not_run)

    assert srv._register_stack_dump_signal() is None


@pytest.mark.parametrize(
    "boom",
    [OSError("unwritable"), RuntimeError("faulthandler unavailable"), ValueError("bad fd")],
)
def test_a_registration_failure_never_takes_the_server_down(monkeypatch, boom):
    """A server that cannot describe itself must still serve. Every failure here
    degrades to `None`; none of them reaches the boot sequence as an exception."""

    def _boom(*a, **kw):
        raise boom

    monkeypatch.setattr(srv.faulthandler, "register", _boom)

    assert srv._register_stack_dump_signal() is None
    assert srv._STACK_DUMP_FILE is None


def test_an_unwritable_svc_dir_degrades_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        srv.breadcrumb, "svc_dir", lambda engine_root=None: (_ for _ in ()).throw(OSError("no dir"))
    )

    assert srv._register_stack_dump_signal() is None


def test_the_dump_file_is_not_the_telemetry_file():
    """`telemetry.jsonl` is one JSON row per server life. A stack dump is
    free-form multi-thread text appended on request; sharing the file would make
    neither parseable."""
    from coordinator_core.warm import telemetry

    assert srv.STACK_DUMP_FILENAME != telemetry.TELEMETRY_FILENAME
