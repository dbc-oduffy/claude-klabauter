"""Regression coverage for C1: the HTTP supervisor's won election must stay
held for this process's lifetime, not be released microseconds after it wins.

Spec backlink: state/dispatch-briefs/2026-08-30-the-warm-restart-stops-being-a-deny/C1.md

WHY THIS FILE EXISTS. Closing the won election handle right after
`election.elect()` wins releases the pipe name back to the OS, so a third
election against the same name succeeds immediately -- see `main()`'s
comment at the `elect()` call site (`supervisor.py`) for the full Win32
argument and the measured figures. The fix holds the handle on
`_ServerContext` and closes it only in `ctx_shutdown` or on the
credential-refusal `return 3` exit path.

NEGATIVE SPEC -- what these tests deliberately do NOT assert:
  * NOT that a lost election is an error -- `test_main_exits_zero_and_
    untouched_when_election_lost` (test_supervisor.py) already pins that a
    loser exits 0 and writes nothing; this file does not duplicate it.
  * NOT the TOCTOU fix in `unlink_discovery`'s owner-checked branch -- that
    is exercised by `test_discovery_unlink_is_ownership_checked.py`; this
    file only confirms the election handle itself is closed in the same
    shutdown sequence as that unlink.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import election, skew, supervisor

pytestmark_win = pytest.mark.skipif(sys.platform != "win32", reason="election.elect is Windows-only")


# ---------------------------------------------------------------------------
# The core defect, isolated from all of main()'s other machinery: does
# holding the won handle open actually exclude a second election, and does
# closing it release the exclusion?
# ---------------------------------------------------------------------------


@pytestmark_win
def test_holding_the_won_handle_excludes_a_second_election(tmp_path: Path) -> None:
    """The measured defect, reproduced at the mechanism level. A second
    `elect()` against the SAME pipe name must lose while the first handle
    is still open -- this is what `main()` relies on now that it no longer
    closes the handle immediately after winning."""
    skew.write_engine_stamp(tmp_path, "sha-single-instance-hold")
    name = supervisor.supervisor_pipe_name(tmp_path)

    handle = election.elect(name)
    try:
        with pytest.raises(election.ElectionLost):
            election.elect(name)
    finally:
        import _winapi

        _winapi.CloseHandle(handle)


# ---------------------------------------------------------------------------
# _ServerContext holds and releases the handle at the right point in its own
# shutdown sequence.
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self) -> None:
        self.closed = False


@pytestmark_win
def test_ctx_shutdown_closes_the_election_handle_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.warm import skew as skew_mod

    class _FakeHttpd:
        def shutdown(self) -> None:
            pass

    class _FakeVersionState:
        server_sha = "sha-fake"

    closed = []

    import _winapi

    def _fake_close(h):
        closed.append(h)

    monkeypatch.setattr(_winapi, "CloseHandle", _fake_close)

    ctx = supervisor._ServerContext(
        httpd=_FakeHttpd(),
        engine_root=None,
        version_state=_FakeVersionState(),
        election_handle="sentinel-handle",
    )
    ctx.ctx_shutdown()

    assert closed == ["sentinel-handle"], "ctx_shutdown must close the held election handle exactly once"
    assert ctx._election_handle is None


@pytestmark_win
def test_ctx_shutdown_is_a_noop_when_no_handle_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A context built without `election_handle=` (e.g. an older caller, or
    a test double) must not raise on shutdown."""

    class _FakeHttpd:
        def shutdown(self) -> None:
            pass

    class _FakeVersionState:
        server_sha = "sha-fake"

    ctx = supervisor._ServerContext(
        httpd=_FakeHttpd(),
        engine_root=None,
        version_state=_FakeVersionState(),
    )
    ctx.ctx_shutdown()  # must not raise


# ---------------------------------------------------------------------------
# The acceptance probe, run in-process with synchronization instead of two
# real subprocesses: a second `main()` invocation against one clone must
# return 0 without writing a discovery record while the first is still
# serving, and only after the first tears down does a fresh election
# succeed again.
# ---------------------------------------------------------------------------


@pytestmark_win
def test_second_main_loses_the_election_while_first_still_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skew.write_engine_stamp(tmp_path, "sha-single-instance-main")
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_declare_execution_route", lambda: None)

    booted = threading.Event()
    release = threading.Event()

    class _BlockingHttpd:
        def __init__(self, addr, handler_cls) -> None:
            self.server_address = ("127.0.0.1", 54329)
            self.RequestHandlerClass = handler_cls

        def serve_forever(self) -> None:
            booted.set()
            release.wait(timeout=10)

        def shutdown(self) -> None:
            release.set()

    import http.server

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _BlockingHttpd)

    write_calls = []
    real_write_discovery = supervisor.write_discovery

    def _tracking_write_discovery(**kwargs):
        write_calls.append(kwargs)
        real_write_discovery(**kwargs)

    monkeypatch.setattr(supervisor, "write_discovery", _tracking_write_discovery)

    first_result = {}

    def _run_first() -> None:
        first_result["rc"] = supervisor.main()

    thread = threading.Thread(target=_run_first, daemon=True)
    thread.start()
    assert booted.wait(timeout=10), "first main() never reached serve_forever"

    # Second invocation, same clone, while the first is still serving.
    second_rc = supervisor.main()

    assert second_rc == 0, "a lost election must exit 0, not deny or raise"
    assert len(write_calls) == 1, "the second, losing invocation must never write a discovery record"

    release.set()
    thread.join(timeout=10)
    assert first_result.get("rc") == 0

    # After the first has torn down (ctx_shutdown ran, closing its held
    # handle), a fresh election against the same name must succeed again --
    # the lock is process-lifetime scoped, not permanently stuck.
    name = supervisor.supervisor_pipe_name(tmp_path)
    handle = election.elect(name)
    import _winapi

    _winapi.CloseHandle(handle)
