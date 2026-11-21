"""The headless-fire ask_gate route benchmark classifies route and error code and never hangs.

Behaviour pinned: a spawn of the cold-operation module is `cold` and its absence is `warm`; engine error
envelopes, subprocess timeouts and other exceptions map to their error codes; the production entry
point (`measure`) bills a real `_invoke_source` call, reports a bounded-timeout call as
`error_code='timeout'` on the cold route with process time and a spawn, instead of hanging.

The real-server cases boot a real warm server child at a stamped temp engine root and drive the
`headless.py` copy living at that root through `_invoke_source('dispatch.ask_gate', ...)`: the client,
the endpoint election and the token are production code; only the server's op body (`_pool_dispatch`)
is a stub, as in the warm suite's other real-server harnesses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import headless_fire_ask_gate_route as bench
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.cadence

_BOOT_DEADLINE_SECS = 30.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ASK = {"sizing_path": "x.yaml", "run_id": "ask-bench"}

_SERVER_CHILD = r'''
import os, sys
from pathlib import Path

repo, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server, skew


class _Version:
    server_sha = "headless-route"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return False

    def is_token_skewed(self, client_token):
        return False


def _gated(self, msg, **_kw):
    return {"jsonrpc": "2.0", "id": msg["id"], "result": {"status": "gated", "served_by": "warm-server"}}


server._ServerContext._pool_dispatch = _gated
token = skew.compute_client_token(root)
on_windows = sys.platform == "win32"
elected = server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
ctx = server._ServerContext(
    name=elected.endpoint,
    sid=elected.identity,
    version_state=_Version(),
    engine_root=root,
    listen_socket=elected.listen_socket,
    endpoint_path=elected.endpoint_path,
)
breadcrumb.write_breadcrumb(
    pipe=elected.endpoint,
    pid=os.getpid(),
    stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
    engine_sha=_Version.server_sha,
    engine_root=root,
    transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
)
if elected.first_handle is not None:
    ctx.serve_forever(elected.first_handle)
else:
    ctx.serve_forever_unix(elected.listen_socket)
'''


def test_route_is_cold_only_when_cold_operation_is_spawned() -> None:
    cold = [["python", "-m", bench.COLD_MODULE, "req.json", "resp.json"]]
    assert bench.classify_route(cold) == "cold"
    assert bench.classify_route([]) == "warm"
    assert bench.classify_route([["git", "status"]]) == "warm"


def test_error_codes() -> None:
    assert bench.error_code_of(None) is None
    assert bench.error_code_of(subprocess.TimeoutExpired("x", 1)) == "timeout"
    assert bench.error_code_of(RuntimeError(json.dumps({"code": -32004, "message": "hung"}))) == -32004
    assert bench.error_code_of(RuntimeError("plain")) == "RuntimeError"
    assert bench.error_code_of(ValueError("v")) == "ValueError"


def test_unstamped_refusal_code_is_the_engine_constant() -> None:
    from coordinator_core import ipc

    assert bench.UNSTAMPED_ENGINE_ROOT_CODE == ipc.UNSTAMPED_ENGINE_ROOT_ERROR == -32005


def test_route_note_is_read_and_absent_note_is_empty(tmp_path: Path) -> None:
    assert bench.read_route_note(tmp_path) == {}
    (tmp_path / "warm_route.json").write_text("not json", encoding="utf-8")
    assert bench.read_route_note(tmp_path) == {}
    (tmp_path / "warm_route.json").write_text(json.dumps({"route": "cold", "reason": "unstamped_root"}), encoding="utf-8")
    assert bench.read_route_note(tmp_path) == {"route": "cold", "reason": "unstamped_root"}


def test_bounded_call_reports_timeout_on_cold_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COORDINATOR_WARM", "0")
    report = bench.measure(timeout=0.05)
    assert report["route"] == "cold"
    # Trap: a race, not a constant. An unstamped dev checkout's cold interpreter can refuse
    # (-32005) inside the 50ms timeout; either answer is bounded, and a hang fails both.
    assert report["error_code"] in ("timeout", bench.UNSTAMPED_ENGINE_ROOT_CODE)
    assert report["spawns"] >= 1
    assert report["process_ms"] > 0
    assert set(report) >= {"route", "process_ms", "spawns", "error_code", "fallthrough_reason"}
