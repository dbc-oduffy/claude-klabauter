"""C2 of docs/plans/2026-08-25-the-http-listener-gets-something-keeping-it-up.md.

Nothing in production ever called `supervisor.ensure_listener()` -- the http
listener's autostart, health-check, and port-discovery entry point -- so
`read_discovery()` returned `None` on every box and p(listener up) was zero
even though `supervisor.py`'s handler was otherwise complete. This pins the
one call site this chunk adds: `warm.server.main()`'s boot path, past its own
election, calling `supervisor.ensure_listener()` and ignoring the result.

AC4 -- the call happens. AC5, the load-bearing one -- the pipe server's own
boot completes UNCHANGED even when discovery is unreadable or
`ensure_listener` itself raises. `ensure_listener` is documented never to
raise (see its own docstring), but this suite does not take that on faith:
it is monkeypatched to raise directly, pinning the call site's OWN fail-open
wrapping rather than trusting the callee's contract.

Harness conventions lifted from `test_server_loop.py` / `test_supervisor.py`:
`tmp_path` as the engine root, `skew.write_engine_stamp` to give it a real
build stamp (`compute_client_token` refuses an unstamped root -- see
`skew.UnstampedEngineRootError`), `monkeypatch` for every injectable seam, no
real pipe server ever left resident. `_run_guarded`'s election and
`serve_forever` calls are monkeypatched to no-ops so this test drives the
real boot sequence -- including the real `supervisor.ensure_listener` call
site -- without blocking in the accept loop or creating a real named pipe.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm import election, server, skew, supervisor
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise


@pytest.fixture(autouse=True)
def _short_warm_runtime_base(monkeypatch: pytest.MonkeyPatch):
    """Overrides the suite-wide HOME quarantine's `warm-runtime-base`
    (`coordinator_core/conftest.py::_quarantine_real_home`) with a short,
    real on-disk root under `/tmp` on POSIX.

    `server.main()`'s real boot path derives a socket path
    (`election.socket_path`) before `_patch_boot_seams` stubs the election
    call itself, and the quarantine's own path is already 90+ bytes deep
    on macOS before `coordinator/warm/<16-hex-hash>/<token>.sock` is
    appended -- tripping `election.SUN_PATH_MAX_BYTES` (100) before this
    module's own boot-sequence assertions run. Same fix as
    `test_election_posix.py::short_runtime_base` (committed b4e300c8f1);
    duplicated here rather than lifted into a shared `conftest.py`
    because this dispatch's scope is this file only.

    `/tmp` does not exist as a drive-relative root on Windows and there
    is no `sun_path` budget to protect there, so `os.name == "nt"` falls
    back to the platform default temp root instead, same guard as
    `conftest.py::_quarantine_real_home` uses for the suite-wide base.
    """
    from coordinator_core.warm import breadcrumb

    base = Path(tempfile.mkdtemp(prefix="wrb-", dir=None if os.name == "nt" else "/tmp"))
    try:
        monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
        yield base
    finally:
        rmtree_or_raise(base, label="warm runtime base")


@pytest.fixture(autouse=True)
def _restore_process_environ():
    """`server.main()` boots as a server process: it scrubs caller-prefixed
    vars (`COORDINATOR_PROBE_*` etc.) and writes the execution route into the
    process's own `os.environ`. Driven in-process here, that mutation would
    outlive the test, so the whole block is restored on teardown.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _stamp(tmp_path: Path) -> None:
    skew.write_engine_stamp(tmp_path, "sha-boot")


def _patch_boot_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    monkeypatch.setattr(server, "_suppress_pool_worker_consoles", lambda: None)
    monkeypatch.setattr(server, "_declare_execution_route", lambda: None)
    # `main()` scrubs this process's own `os.environ` at boot; run in-process it would
    # delete the harness's ambient vars (e.g. COORDINATOR_PROBE_CANARY) for the session.
    monkeypatch.setattr(server, "_scrub_test_harness_env", lambda: None)
    monkeypatch.setattr(election, "elect", lambda name, user_sid=None: 1)
    monkeypatch.setattr(election, "elect_unix_socket", lambda path: object())
    monkeypatch.setattr(server._ServerContext, "serve_forever", lambda self, handle: None)
    monkeypatch.setattr(server._ServerContext, "serve_forever_unix", lambda self, sock: None)


def test_main_boot_path_calls_ensure_listener(tmp_path, monkeypatch):
    _stamp(tmp_path)
    _patch_boot_seams(monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or None,
    )

    result = server.main()

    assert result == 0
    assert calls == [tmp_path], "ensure_listener must be called exactly once, with the boot's own engine root"


def test_pipe_server_boots_unchanged_when_ensure_listener_raises(tmp_path, monkeypatch):
    _stamp(tmp_path)
    _patch_boot_seams(monkeypatch, tmp_path)

    def _boom(root=None, **kwargs):
        raise OSError("discovery file unreadable")

    monkeypatch.setattr(supervisor, "ensure_listener", _boom)

    # `main()` dispatches on WHICH ENDPOINT WON the election (server.py's own
    # comment at the `serve_forever`/`serve_forever_unix` branch), not on a
    # platform read: on the POSIX box this suite actually runs on, that is
    # `serve_forever_unix`, never `serve_forever` (the Windows named-pipe
    # arm `_patch_boot_seams` stubs `election.elect` for but never wins on
    # this platform). Both are patched so the assertion below is the
    # platform-appropriate one rather than one hard-coded to Windows.
    served = []
    monkeypatch.setattr(
        server._ServerContext, "serve_forever", lambda self, handle: served.append(handle)
    )
    monkeypatch.setattr(
        server._ServerContext, "serve_forever_unix", lambda self, sock: served.append(sock)
    )

    result = server.main()

    assert result == 0
    assert len(served) == 1, "the pipe server must still reach its serve_forever arm, unchanged, after ensure_listener raised"


def test_pipe_server_boots_unchanged_when_discovery_is_unreadable(tmp_path, monkeypatch):
    _stamp(tmp_path)
    _patch_boot_seams(monkeypatch, tmp_path)

    # `should_spawn` is pinned because the real one claims `warm-http.json.boot.lock`
    # via `breadcrumb.try_claim_boot`, whose fd is deliberately leaked for the
    # process's life; on Windows that open handle makes the base fixture's
    # teardown rmtree fail with `CloneTeardownLeak`. An absent record yields
    # True from the real one, so this is the same decision without the leak.
    monkeypatch.setattr(supervisor, "should_spawn", lambda *a, **kw: True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: False)

    served = []
    monkeypatch.setattr(
        server._ServerContext, "serve_forever", lambda self, handle: served.append(handle)
    )
    monkeypatch.setattr(
        server._ServerContext, "serve_forever_unix", lambda self, sock: served.append(sock)
    )

    result = server.main()

    assert result == 0
    assert len(served) == 1
