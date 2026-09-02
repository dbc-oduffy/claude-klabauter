"""The HTTP supervisor's boot election on POSIX -- the arm that did not exist
until 2026-09-02, and the reason no test noticed.

WHAT WAS BROKEN. `supervisor.main()`'s step 1 called
`election.current_user_sid()` unconditionally. That function raises
`RuntimeError("current_user_sid is Windows-only")` off Windows, so on this box
(and every POSIX box) EVERY supervisor `ensure_listener` spawned died before
binding a port and before writing `warm-http.json`. The failure was silent in
both directions: `spawn_detached` DEVNULLs the child's stdio and never reads its
exit code, and `ensure_listener` is fail-open by contract, so the only
observable was `ensure_listener` returning `None` forever while every hot-path
invocation fell through to cold dispatch. Measured 2026-09-02 against this
box's published engine root (`machine-local get repos.claude_klabauter`):
`warm.json` (the pipe/unix server's breadcrumb) present and its pid serving,
`warm-http.json` never once written.

WHY NOTHING CAUGHT IT. Every existing test that drives this election --
`test_supervisor.py :: test_main_exits_zero_and_untouched_when_election_lost`,
all of `test_http_listener_is_single_instance.py` -- is
`skipif(sys.platform != "win32")`, because the primitive it drove really was
Windows-only. The skip was correct and the coverage it left behind was empty:
`warm.server` grew its POSIX election arm on 2026-08-21 and this module never
did, with nothing red to say so. This file is the POSIX half, so the same
regression cannot recur on the platform the engine actually runs on here.

Negative-spec:
    - Does NOT re-test the Windows arm. `test_http_listener_is_single_instance.py`
      owns it and still runs there; this file is `skipif(win32)`, the exact
      mirror, so the two together cover both arms and neither duplicates the
      other.
    - Does NOT bind a real TCP port or serve a real request. The election is
      the subject; `ThreadingHTTPServer` is replaced with a blocking double
      exactly as the Windows file's own acceptance probe does, so the test
      costs no listener and no port on a box running 50+ sessions.
    - Does NOT assert the discovery record's CONTENT beyond its existence --
      `test_supervisor.py` and `test_discovery_write_is_atomic.py` own the
      record's shape. What this file adds is that a POSIX boot reaches the
      write at all.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import election, skew, supervisor

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX election arm")


class _BlockingHttpd:
    """`ThreadingHTTPServer` stand-in that reports a bound address and parks
    in `serve_forever` until released -- lifted in shape from
    `test_http_listener_is_single_instance.py`'s own double so both platforms'
    acceptance probes drive `main()` the same way."""

    def __init__(self, addr, handler_cls) -> None:
        self.server_address = ("127.0.0.1", 54331)
        self.RequestHandlerClass = handler_cls
        self.booted = threading.Event()
        self.release = threading.Event()

    def serve_forever(self) -> None:
        self.booted.set()
        self.release.wait(timeout=10)

    def shutdown(self) -> None:
        self.release.set()


def _install_doubles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str) -> list:
    """Stamp `tmp_path` as an engine root and point `main()` at it, with the
    listener and the route declaration doubled out. Returns the list every
    `write_discovery` call is appended to."""
    import http.server

    skew.write_engine_stamp(tmp_path, stamp)
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_declare_execution_route", lambda: None)

    servers: list = []
    created = threading.Event()

    def _make_httpd(addr, handler_cls):
        httpd = _BlockingHttpd(addr, handler_cls)
        servers.append(httpd)
        created.set()
        return httpd

    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _make_httpd)

    write_calls: list = []
    real_write_discovery = supervisor.write_discovery

    def _tracking_write_discovery(**kwargs):
        write_calls.append(kwargs)
        real_write_discovery(**kwargs)

    monkeypatch.setattr(supervisor, "write_discovery", _tracking_write_discovery)
    return servers, created, write_calls


def test_main_elects_and_publishes_a_discovery_record_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression itself: `main()` must reach `serve_forever` and publish
    a discovery record on POSIX.

    Before the POSIX election arm landed this raised
    `RuntimeError: current_user_sid is Windows-only` out of step 1, which is
    what made `ensure_listener` unable to ever produce a listener on this
    platform."""
    servers, created, write_calls = _install_doubles(tmp_path, monkeypatch, "sha-posix-election")

    result: dict = {}

    def _run() -> None:
        try:
            result["rc"] = supervisor.main()
        except BaseException as exc:  # noqa: BLE001 -- the failure IS the finding
            result["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    booted = created.wait(timeout=10) and servers[0].booted.wait(timeout=10)

    try:
        assert "exc" not in result, f"main() raised on POSIX instead of electing: {result.get('exc')!r}"
        assert booted, "main() never reached serve_forever on POSIX"
        assert len(write_calls) == 1, "a booted supervisor must publish exactly one discovery record"
        assert supervisor.read_discovery(tmp_path) is not None
    finally:
        for server in servers:
            server.shutdown()
        thread.join(timeout=10)


def test_second_main_loses_the_election_while_the_first_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The election must actually EXCLUDE on POSIX, not merely stop raising:
    a second `main()` against the same clone exits 0 and writes nothing while
    the first holds the lock.

    Same-process second call, deliberately -- `flock` is per open file
    description, so a second `os.open` here conflicts exactly as another
    process's would (`breadcrumb.try_claim_boot` leans on the identical
    property), which lets this probe the real exclusion without a second
    interpreter."""
    servers, created, write_calls = _install_doubles(
        tmp_path, monkeypatch, "sha-posix-election-second"
    )

    thread = threading.Thread(target=supervisor.main, daemon=True)
    thread.start()
    booted = created.wait(timeout=10) and servers[0].booted.wait(timeout=10)

    try:
        assert booted, "first main() never reached serve_forever"
        assert supervisor.main() == 0, "a lost election must exit 0, not deny or raise"
        assert len(write_calls) == 1, "the losing invocation must never write a discovery record"
    finally:
        for server in servers:
            server.shutdown()
        thread.join(timeout=10)

    # The lock is process-lifetime scoped, not permanently stuck: once the
    # first has torn down (`ctx_shutdown` released the fd), the same path
    # elects again.
    handle = election.elect_exclusive_lock(supervisor.supervisor_lock_path(tmp_path))
    election.release_exclusive_lock(handle)
