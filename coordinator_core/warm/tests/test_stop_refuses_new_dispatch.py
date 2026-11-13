"""A warm-engine-stop frame is refused before dispatch (-32007) so the door runs it cold;
it is never served in a pool worker that the stop would wait on."""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm import server

pytestmark = [pytest.mark.cadence]

_NOT_WARM_LOADABLE = -32007


class _FakeIO:
    def __init__(self, lines):
        self._lines = list(lines)
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _NotSkewed:
    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


def _from_argv_frame(entrypoint: str) -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "invoke.from_argv",
        "params": {"argv": [], "cwd": ".", "entrypoint": entrypoint},
        "_engine_token": "client-token",
    }
    return (json.dumps(msg) + "\n").encode("utf-8")


def _serve(entrypoint: str):
    dispatched: list[dict] = []

    def _dispatch(msg, **_kwargs):
        dispatched.append(msg)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"exit_code": 0}}

    io_obj = _FakeIO([_from_argv_frame(entrypoint)])
    in_flight = server.InFlightCounter()
    server._handle_connection(
        io_obj,
        version_state=_NotSkewed(),
        server_sha="x",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=in_flight,
        dispatch=_dispatch,
    )
    return dispatched, [json.loads(line) for line in io_obj.written]


def test_warm_engine_stop_is_refused_undispatched():
    dispatched, [response] = _serve("warm-engine-stop")

    assert dispatched == []
    assert response["error"]["code"] == _NOT_WARM_LOADABLE
    assert response["id"] == 7


def test_other_entrypoint_still_dispatches():
    dispatched, [response] = _serve("whats-next")

    assert len(dispatched) == 1
    assert "error" not in response


def test_refusal_code_is_the_ipc_not_warm_loadable_code():
    from coordinator_core.ipc import ENTRYPOINT_NOT_WARM_LOADABLE_ERROR

    assert server._ENTRYPOINT_NOT_WARM_LOADABLE_ERROR == ENTRYPOINT_NOT_WARM_LOADABLE_ERROR
