"""Inside a publish swap window an own-generation request is served; a token-skewed one is refused ENGINE_SKEW without eviction."""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

from coordinator_core import _engine_landing as el
from coordinator_core.warm import server, skew


class _IO:
    def __init__(self, line: bytes):
        self._line = line
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        return self._line

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class _VersionState:
    server_sha = "abc"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


def _run(root, version_state=None):
    line = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}, "_engine_token": "t"}) + "\n").encode()
    io = _IO(line)
    events: list[str] = []
    dispatched: list[object] = []
    server._handle_connection(
        io,
        version_state=version_state or _VersionState(),
        server_sha="abc",
        close_listener=lambda: events.append("close"),
        drain=lambda: events.append("drain"),
        in_flight=server.InFlightCounter(),
        dispatch=lambda msg, **kw: dispatched.append(msg) or {"jsonrpc": "2.0", "id": 1, "result": "ok"},
        record_exit=lambda reason, detail=None: events.append("exit"),
        engine_root=root,
    )
    return [json.loads(w) for w in io.written], events, dispatched


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


class _SkewedVersionState(_VersionState):
    def is_skewed(self, client_token: str) -> bool:
        return True


def test_live_marker_refuses_token_skewed_without_eviction(tmp_path):
    root = _root(tmp_path)
    el.begin_swap(root, deadline_s=30)
    responses, events, dispatched = _run(root, _SkewedVersionState())
    assert responses[0]["error"]["code"] == skew.ENGINE_SKEW
    assert events == []
    assert dispatched == []


def test_live_marker_serves_own_generation_request(tmp_path):
    root = _root(tmp_path)
    el.begin_swap(root, deadline_s=30)
    responses, events, dispatched = _run(root)
    assert responses[0]["result"] == "ok"
    assert events == []
    assert len(dispatched) == 1


def test_every_version_state_attribute_server_calls_is_defined_on_server_version_state():
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if (isinstance(owner, ast.Name) and owner.id == "version_state") or (
            isinstance(owner, ast.Attribute) and owner.attr == "version_state"
        ):
            used.add(node.attr)
    assert used, "AST walk found no version_state attribute use; the walk is broken"
    skew_tree = ast.parse(Path(skew.__file__).read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(skew_tree) if isinstance(n, ast.ClassDef) and n.name == "ServerVersionState"
    )
    defined = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    defined |= {
        t.id
        for n in cls.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    defined |= {
        n.attr
        for n in ast.walk(cls)
        if isinstance(n, ast.Attribute)
        and isinstance(n.ctx, ast.Store)
        and isinstance(n.value, ast.Name)
        and n.value.id == "self"
    }
    missing = sorted(used - defined)
    assert not missing, f"server.py uses version_state.{missing} not defined on ServerVersionState"


def test_absent_marker_dispatches(tmp_path):
    responses, events, dispatched = _run(_root(tmp_path))
    assert responses[0]["result"] == "ok"
    assert len(dispatched) == 1


def test_stale_marker_dispatches(tmp_path):
    root = _root(tmp_path)
    (root / ".git" / el.MARKER_NAME).write_text(
        json.dumps({"deadline_epoch": time.time() - 5}), encoding="utf-8"
    )
    responses, _, dispatched = _run(root)
    assert responses[0]["result"] == "ok"
    assert len(dispatched) == 1


def test_untrusted_caller_does_not_learn_landing_state(tmp_path):
    root = _root(tmp_path)
    el.begin_swap(root, deadline_s=30)
    io = _IO((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "noop", "params": {}}) + "\n").encode())
    server._handle_connection(
        io,
        version_state=_VersionState(),
        server_sha="abc",
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        engine_root=root,
    )
    assert json.loads(io.written[0])["error"]["code"] != skew.ENGINE_SKEW
