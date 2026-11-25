"""A send to a named Claude session resolves its live pipe, never a stale row's dead one (Windows)."""
import importlib.util
import json
import os
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_router_live_pipe", BIN / "unimatrix-peer-router.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

pytestmark = [
    pytest.mark.complete,
    pytest.mark.skipif(os.name != "nt", reason="named pipes are Windows-only"),
]


@pytest.fixture
def pipes():
    """(dead endpoint, live endpoint): the live one backed by a real named pipe with a test-unique name."""
    win32pipe = pytest.importorskip("win32pipe")
    tag = uuid.uuid4().hex
    dead = "\\\\.\\pipe\\LOCAL\\cc-msg-dead" + tag
    live = "\\\\.\\pipe\\LOCAL\\cc-msg-live" + tag
    handle = win32pipe.CreateNamedPipe(
        live, win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
        win32pipe.PIPE_UNLIMITED_INSTANCES, 4096, 4096, 0, None)
    try:
        yield dead, live
    finally:
        handle.Close()


def _router(tmp_path, rows):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for i, row in enumerate(rows):
        (sessions / f"row{i}.json").write_text(json.dumps(
            {"name": "gem", "pid": os.getpid(), "sessionId": f"sid-{i}", **row}))
    return pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps", adapters={})


def test_the_live_pipe_wins_over_a_stale_rows_dead_pipe(tmp_path, pipes):
    dead, live = pipes
    router = _router(tmp_path, [{"messagingSocketPath": dead, "startedAt": 2},
                                {"messagingSocketPath": live, "startedAt": 1}])
    assert router.resolve_sender("gem") == live
    resp = router.resolve_send_target("gem")
    assert resp["ok"] is True
    assert resp["endpoint"] == live


def test_a_name_with_only_a_dead_pipe_still_refuses(tmp_path, pipes):
    dead, _ = pipes
    router = _router(tmp_path, [{"messagingSocketPath": dead, "startedAt": 1}])
    resp = router.resolve_send_target("gem")
    assert resp["ok"] is False
    assert "no unique live session named gem" in resp["error"]


def test_two_live_pipes_for_one_name_stay_ambiguous(tmp_path, pipes):
    _, live = pipes
    win32pipe = pytest.importorskip("win32pipe")
    other = live + "-other"
    handle = win32pipe.CreateNamedPipe(
        other, win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
        win32pipe.PIPE_UNLIMITED_INSTANCES, 4096, 4096, 0, None)
    try:
        router = _router(tmp_path, [{"messagingSocketPath": live, "startedAt": 1},
                                    {"messagingSocketPath": other, "startedAt": 2}])
        assert router.resolve_sender("gem") == ""
        assert "ambiguous" in router.last_refusal
    finally:
        handle.Close()
