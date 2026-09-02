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

import shutil
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm import election, server, skew, supervisor


@pytest.fixture(autouse=True)
def _short_warm_runtime_base(monkeypatch: pytest.MonkeyPatch):
    """Overrides the suite-wide HOME quarantine's `warm-runtime-base`
    (`coordinator_core/conftest.py::_quarantine_real_home`) with a short,
    real on-disk root under `/tmp`.

    `server.main()`'s real boot path derives a socket path
    (`election.socket_path`) before `_patch_boot_seams` stubs the election
    call itself, and the quarantine's own path is already 90+ bytes deep
    on macOS before `coordinator/warm/<16-hex-hash>/<token>.sock` is
    appended -- tripping `election.SUN_PATH_MAX_BYTES` (100) before this
    module's own boot-sequence assertions run. Same fix as
    `test_election_posix.py::short_runtime_base` (committed b4e300c8f1);
    duplicated here rather than lifted into a shared `conftest.py`
    because this dispatch's scope is this file only.
    """
    from coordinator_core.warm import breadcrumb

    base = Path(tempfile.mkdtemp(prefix="wrb-", dir="/tmp"))
    try:
        monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(base))
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _stamp(tmp_path: Path) -> None:
    skew.write_engine_stamp(tmp_path, "sha-boot")


def _patch_boot_seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralize every part of `_run_guarded`'s boot sequence this test does
    not exercise: the real election (would create a live named pipe / unix
    socket), the op-registry eager import (703ms, irrelevant here), console
    suppression (Windows-only side effect), and the accept loop itself
    (`serve_forever` never returns under real traffic)."""
    monkeypatch.setattr(server, "_engine_clone_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    monkeypatch.setattr(server, "_suppress_pool_worker_consoles", lambda: None)
    # `_declare_execution_route` writes a real `os.environ` entry as its own
    # documented boot step (server.py's own docstring) -- orthogonal to this
    # chunk, and the process-env leak guard flags any test that leaves it
    # set. Stubbed rather than exercised, since real env mutation is not
    # what this suite is pinning.
    monkeypatch.setattr(server, "_declare_execution_route", lambda: None)
    monkeypatch.setattr(election, "elect", lambda name, user_sid=None: 1)
    monkeypatch.setattr(election, "elect_unix_socket", lambda path: object())
    monkeypatch.setattr(server._ServerContext, "serve_forever", lambda self, handle: None)
    monkeypatch.setattr(server._ServerContext, "serve_forever_unix", lambda self, sock: None)


def test_main_boot_path_calls_ensure_listener(tmp_path, monkeypatch):
    """AC4: `main()`'s boot path reaches `supervisor.ensure_listener()`,
    called with this process's own resolved engine-clone root, after the
    election that seam's `_elect_windows_pipe`/`_elect_unix_socket_endpoint`
    already won -- asserted by spying on the call, not by asserting a real
    listener bound (this test never starts one)."""
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
    """AC5, the load-bearing one. A pipe server that fails to boot because an
    http listener could not start has inverted the guarantee this chunk
    exists to provide. Pins the call site's OWN fail-open wrapping: even
    though `ensure_listener` is documented to return `None` on every failure
    mode and never raise, this simulates it raising anyway and asserts the
    pipe server's boot -- election already won, breadcrumb write, execution
    route declaration, op-registry preload, and the handoff into
    `serve_forever` -- completes exactly as it would have with no http
    listener call in the boot path at all."""
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
    """The AC5 scenario named literally: an unreadable discovery file (not a
    raise) still yields `ensure_listener() is None` by that function's own
    fail-open contract, and the pipe server's boot must complete unchanged --
    the sibling case to the raising one above, exercised through the real
    (unmonkeypatched) `supervisor.ensure_listener` against a genuinely
    missing/corrupt discovery file rather than a stand-in."""
    _stamp(tmp_path)
    _patch_boot_seams(monkeypatch, tmp_path)

    # No discovery file has ever been written under this tmp_path root, so
    # `supervisor.read_discovery` returns None and `ensure_listener` takes
    # its fail-open "nothing to spawn from, return None this call" path --
    # this also exercises `should_spawn`, so stub the actual spawn out.
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: False)

    # Both arms patched -- see the sibling test above for why the assertion
    # is on the platform-appropriate arm rather than the Windows one.
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
