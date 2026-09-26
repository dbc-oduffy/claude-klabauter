"""A warm server must refuse, never answer, a request naming a settings home it
does not serve.

Bug backlog: state/bug-backlog/2026-08-29-the-warm-server-answers-against-its-spaw-
f1bcc4154ca4.yaml (P0) -- step 1 of that row's two-step disposition. The server
resolved `settings_home()` once, at spawn time, and its identity key is (user,
engine-clone, engine-token): the home is not in it. A caller that set
`COORDINATOR_SETTINGS_HOME` was therefore answered against a home it did not name,
with no error and no warning -- verified 2026-08-29 through both
`coordinator-invoke.cmd` and `coordinator-invoke.exe`.

WHY THESE TESTS ARE ABOUT AUTHORIZATION, NOT TEST ISOLATION. `bash_guards/
_blanket_disarm.py :: marker_path()` resolves the blanket-disarm marker -- a file
whose PRESENCE turns guards off -- through `settings_home()`; `authz/
classification.py` keys op authorization off it; `secrets/` is a directory inside
it. A silently-wrong home on those paths answers in the direction that DISARMS.

WHAT IS NOT TESTED HERE, because it is not built: isolation. A warm server still
serves exactly one settings home. Step 2 (per-request resolution over the request
envelope, following `warm/caller_context.py`) is plan-sized and owns that.

LOCUS MOVED (C2, docs/plans/2026-08-31-the-settings-home-crosses-the-warm-boundary.md).
The refusal used to live in `_serve_line`, ahead of every `dispatch=` call regardless
of which one was bound. It now lives in `_run_dispatch` itself, gated on
`isolated=False`, so it is reached by every unisolated dispatch leg (the default
`dispatch=` `_handle_connection` carries, AND `_pool_dispatch`'s own
`BrokenProcessPool` fallback) rather than only the one `_serve_line` happened to sit in
front of. `_serve_line` itself now only pops the claim and joins it onto `caller`
(`caller.settings_home`) -- a test that injects a fake `dispatch=` callable (as most of
this module's own tests do, for speed) no longer sees a refusal at all, because the
fake callable IS the mismatch check's new home and simply does not implement it. The
"served exactly as before" tests below therefore assert PASS-THROUGH to a fake
dispatch, never a refusal; the refusal itself is asserted directly against
`_run_dispatch`/`_pool_dispatch`, and once more through the default-dispatch leg with
no `dispatch=` override at all.

Reuses `test_server_loop.py`'s `_FakeIO` / `_FakeVersionState` / `_frame` helpers,
same as `test_untrusted_caller_token.py` -- no real pipe needed to exercise
`_serve_line`, and the module imports on non-Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from coordinator_core.warm import caller_context, server, settings_home_claim
from coordinator_core.warm.tests.test_server_loop import _FakeIO, _FakeVersionState, _frame


@pytest.fixture(autouse=True)
def _short_warm_runtime_base(monkeypatch: pytest.MonkeyPatch):
    """Overrides the suite-wide HOME quarantine's `warm-runtime-base`
    (`coordinator_core/conftest.py::_quarantine_real_home`) with a short,
    real on-disk root under `/tmp` on POSIX.

    Only `_sent_request`'s two tests below drive the real
    `client.try_warm_dispatch` preamble (`election.socket_path`), but the
    quarantine's own path is already 90+ bytes deep on macOS before
    `coordinator/warm/<16-hex-hash>/<token>.sock` is appended, tripping
    `election.SUN_PATH_MAX_BYTES` (100) before those tests' own
    assertions run. Applied autouse for uniformity with this dispatch's
    sibling files. Same fix as `test_election_posix.py::
    short_runtime_base` (committed b4e300c8f1); duplicated here rather
    than lifted into a shared `conftest.py` because this dispatch's scope
    is this file only.

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
        shutil.rmtree(base, ignore_errors=True)


def _serve(frame: bytes, dispatch) -> _FakeIO:
    io_obj = _FakeIO([frame])
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=False),
        server_sha="deadbeef",
        close_listener=lambda: pytest.fail("must not close the listener"),
        drain=lambda: pytest.fail("must not drain"),
        in_flight=server.InFlightCounter(),
        dispatch=dispatch,
    )
    return io_obj


def _ok_dispatch(seen: list):
    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        seen.append(msg)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    return _dispatch


def test_mismatched_settings_home_is_refused_at_run_dispatch(monkeypatch):
    from coordinator_core import ipc

    monkeypatch.setattr(
        ipc, "dispatch_message", lambda *a, **k: pytest.fail("dispatch_message must never run")
    )

    other_home = os.path.join(os.sep, "nowhere", "some-other-settings-home")
    caller = replace(caller_context.resolve_caller_context(), settings_home=other_home)
    response = server._run_dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
        caller=caller,
        isolated=False,
    )

    assert response["error"]["code"] == server.SETTINGS_HOME_MISMATCH_ERROR
    assert response["id"] == 2


def test_the_default_dispatch_leg_is_refused_too(monkeypatch):
    from coordinator_core import ipc

    monkeypatch.setattr(
        ipc, "dispatch_message", lambda *a, **k: pytest.fail("dispatch_message must never run")
    )

    other_home = os.path.join(os.sep, "nowhere", "default-leg-home")
    io_obj = _FakeIO(
        [_frame(id_=9, method="ping", extra={settings_home_claim.SETTINGS_HOME_FIELD: other_home})]
    )
    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=False),
        server_sha="deadbeef",
        close_listener=lambda: pytest.fail("must not close the listener"),
        drain=lambda: pytest.fail("must not drain"),
        in_flight=server.InFlightCounter(),
    )

    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] == server.SETTINGS_HOME_MISMATCH_ERROR


def test_old_locus_no_longer_refuses_on_its_own():
    seen: list = []
    other_home = os.path.join(os.sep, "nowhere", "old-locus-should-not-refuse")
    io_obj = _serve(
        _frame(id_=11, method="ping", extra={settings_home_claim.SETTINGS_HOME_FIELD: other_home}),
        _ok_dispatch(seen),
    )

    assert len(seen) == 1
    assert json.loads(io_obj.written[0])["result"] == "ok"


def test_refusal_names_both_homes(monkeypatch):
    from coordinator_core import ipc

    monkeypatch.setattr(
        ipc, "dispatch_message", lambda *a, **k: pytest.fail("dispatch_message must never run")
    )
    other_home = os.path.join(os.sep, "nowhere", "some-other-settings-home")
    io_obj = _serve(
        _frame(id_=1, method="ping", extra={settings_home_claim.SETTINGS_HOME_FIELD: other_home}),
        server._run_dispatch,
    )

    from coordinator_core._settings_home import settings_home

    message = json.loads(io_obj.written[0])["error"]["message"]
    assert other_home in message
    assert str(settings_home()) in message


def test_no_claim_is_served_exactly_as_before():
    """BACKWARD COMPATIBILITY IS THE POINT. Absence is not a mismatch: the
    ordinary invocation sets no override, stamps no field, and must reach
    `dispatch` untouched. A fix that refuses on absence would take the whole
    fleet down, since every user-path call omits the field.
    """
    seen: list = []
    io_obj = _serve(_frame(id_=7, method="ping"), _ok_dispatch(seen))

    assert len(seen) == 1
    assert json.loads(io_obj.written[0])["result"] == "ok"


def test_matching_claim_is_served_and_the_field_never_reaches_the_op():
    seen: list = []
    from coordinator_core._settings_home import settings_home

    io_obj = _serve(
        _frame(
            id_=3,
            method="ping",
            extra={settings_home_claim.SETTINGS_HOME_FIELD: str(settings_home())},
        ),
        _ok_dispatch(seen),
    )

    assert len(seen) == 1
    assert settings_home_claim.SETTINGS_HOME_FIELD not in seen[0]
    assert json.loads(io_obj.written[0])["result"] == "ok"


def test_mismatch_does_not_evict_the_shared_server():
    other_home = os.path.join(os.sep, "nowhere", "elsewhere")
    io_obj = _serve(
        _frame(id_=4, method="ping", extra={settings_home_claim.SETTINGS_HOME_FIELD: other_home}),
        server._run_dispatch,
    )

    assert json.loads(io_obj.written[0])["error"]["code"] == server.SETTINGS_HOME_MISMATCH_ERROR


# THE BROKEN-POOL FALLBACK ORDERING (Finding 2). `_op_may_mutate`'s diversion
# MUTATING op with a mismatched settings-home claim must come back as
# `WARM_DISPATCH_INDETERMINATE` (-32004), never `SETTINGS_HOME_MISMATCH_ERROR`


def test_broken_pool_fallback_never_lets_a_mutating_op_reach_the_settings_home_gate(monkeypatch):
    """Pins the ordering Finding 2 depends on: `_op_may_mutate` diverts a
    MUTATING op to the indeterminate envelope before `_run_dispatch` --
    and therefore the new settings-home gate inside it -- is ever reached,
    even when the request also carries a mismatched claim.
    """
    from concurrent.futures.process import BrokenProcessPool

    class _BrokenPool:
        def submit(self, *_a, **_k):
            raise BrokenProcessPool("dead worker")

        def shutdown(self, wait=True):
            pass

    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool = _BrokenPool()
    import threading as _threading

    ctx._dispatch_pool_lock = _threading.Lock()

    monkeypatch.setattr(
        server,
        "_run_dispatch",
        lambda *a, **k: pytest.fail(
            "_run_dispatch must not run for a MUTATING op after a dead worker"
        ),
    )

    other_home = os.path.join(os.sep, "nowhere", "pool-fallback-home")
    caller = replace(caller_context.resolve_caller_context(), settings_home=other_home)

    result = ctx._pool_dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "ceremony.scoped_git_commit"},
        caller=caller,
    )

    assert result["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert result["error"]["code"] != server.SETTINGS_HOME_MISMATCH_ERROR
    assert result["id"] == 5


def test_caller_claims_nothing_without_an_explicit_override(monkeypatch):
    monkeypatch.delenv(settings_home_claim.SETTINGS_HOME_ENV, raising=False)
    assert settings_home_claim.caller_claim() is None


def test_caller_claim_is_the_resolved_home(monkeypatch, tmp_path):
    monkeypatch.setenv(settings_home_claim.SETTINGS_HOME_ENV, str(tmp_path))

    from coordinator_core._settings_home import settings_home

    assert settings_home_claim.caller_claim() == str(settings_home())


def test_absent_claim_agrees_with_anything():
    assert settings_home_claim.claims_agree(None, os.path.join(os.sep, "anything"))


def test_spelling_differences_are_not_different_homes(tmp_path):
    home = str(tmp_path)
    assert settings_home_claim.claims_agree(home + os.sep, home)
    assert settings_home_claim.claims_agree(home.replace("\\", "/"), home)
    if os.name == "nt":
        assert settings_home_claim.claims_agree(home.upper(), home)


def test_a_genuinely_different_home_disagrees(tmp_path):
    assert not settings_home_claim.claims_agree(str(tmp_path / "a"), str(tmp_path / "b"))


class _FakePipe:

    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    def close(self) -> None:
        pass


def _sent_request(monkeypatch) -> dict:
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    pipe = _FakePipe()
    monkeypatch.setattr(client, "_open_pipe", lambda name: pipe)
    client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    return json.loads(pipe.written[0])


def test_client_stamps_the_home_it_was_told_to_use(monkeypatch, tmp_path):
    monkeypatch.setenv(settings_home_claim.SETTINGS_HOME_ENV, str(tmp_path))

    from coordinator_core._settings_home import settings_home

    request = _sent_request(monkeypatch)
    assert request[settings_home_claim.SETTINGS_HOME_FIELD] == str(settings_home())


def test_client_stamps_nothing_when_no_home_was_named(monkeypatch):
    monkeypatch.delenv(settings_home_claim.SETTINGS_HOME_ENV, raising=False)
    assert settings_home_claim.SETTINGS_HOME_FIELD not in _sent_request(monkeypatch)


# EXIT CRITERION 3 -- the no-claim hot path pays no resolution, asserted
# STRUCTURALLY rather than by timing. A ~50-session box's timing noise cannot


def _counting_settings_home(monkeypatch, calls: list):
    import coordinator_core._settings_home as _sh

    real = _sh.settings_home

    def _counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(_sh, "settings_home", _counted)


def test_a_no_claim_request_never_resolves_this_servers_home(monkeypatch):
    from coordinator_core import ipc

    async def _ok(msg, *, caller=None, corr_id=None):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    monkeypatch.setattr(ipc, "dispatch_message", _ok)

    caller = caller_context.resolve_caller_context()
    assert caller.settings_home is None, "fixture precondition: no claim carried"

    calls: list = []
    _counting_settings_home(monkeypatch, calls)

    server._run_dispatch(
        {"jsonrpc": "2.0", "id": 20, "method": "ping", "params": {}},
        caller=caller,
        isolated=False,
    )

    assert calls == [], "no-claim request resolved the server's own home"


def test_the_uncalled_assertion_discriminates(monkeypatch):
    other_home = os.path.join(os.sep, "nowhere", "some-other-settings-home")
    caller = replace(caller_context.resolve_caller_context(), settings_home=other_home)

    calls: list = []
    _counting_settings_home(monkeypatch, calls)

    server._run_dispatch(
        {"jsonrpc": "2.0", "id": 21, "method": "ping", "params": {}},
        caller=caller,
        isolated=False,
    )

    assert calls, "the probe counted nothing even with a claim carried"
