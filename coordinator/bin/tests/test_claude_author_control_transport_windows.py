"""Tests for claude_author_control/transport_windows.py (named-pipe transport)."""
from __future__ import annotations

import errno
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from claude_author_control import protocol as p  # noqa: E402
from claude_author_control import transport_windows as tw  # noqa: E402

win_only = pytest.mark.skipif(sys.platform != "win32", reason="named pipes are win32-only")


def test_import_safe_everywhere():
    assert hasattr(tw, "PipeTransport")


def test_sddl_is_protected_single_sid():
    assert tw.sddl_for_sid("S-1-5-21-1") == "D:P(A;;GA;;;S-1-5-21-1)"


def test_pipe_name_validates_instance():
    assert tw.pipe_name("0123456789ab") == p.PIPE_PREFIX + "0123456789ab"
    with pytest.raises(ValueError):
        tw.pipe_name("../evil")


@pytest.mark.skipif(sys.platform == "win32", reason="non-windows refusal")
def test_construction_refused_off_windows():
    with pytest.raises(OSError):
        tw.PipeTransport("0123456789ab")


def _rpc(name: str, payload: dict) -> dict:
    # A pipe client retries while no instance is listening (between instances the name is
    # absent, or busy, which the CRT reports as EINVAL); the drive-root guard's own path probe can briefly consume an instance.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            f = open(name, "r+b", buffering=0)
            break
        except OSError as e:
            if e.errno not in (errno.ENOENT, errno.EINVAL, errno.EBUSY) or time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    with f:
        f.write(json.dumps(payload).encode() + b"\n")
        return json.loads(f.readline())


@win_only
def test_round_trip_stamps_pid_and_sid():
    import os

    inst = uuid.uuid4().hex[:12]
    t = tw.PipeTransport(inst)
    seen = {}

    def handler(req, peer):
        seen["peer"] = peer
        return p.ok_response(req.id, identity=p.identity_for(peer, req.on_behalf_of))

    th = threading.Thread(target=t.serve, args=(handler,), daemon=True)
    th.start()
    try:
        resp = _rpc(t.address, {"v": 0, "id": 1, "op": "status", "args": {}})
        assert resp["ok"] is True
        assert seen["peer"].pid == os.getpid()
        assert seen["peer"].sid == t._own_sid
        assert resp["identity"]["transport"] == "pipe"
    finally:
        t.close()


@win_only
def test_first_instance_blocks_squatter():
    inst = uuid.uuid4().hex[:12]
    t = tw.PipeTransport(inst)
    try:
        with pytest.raises(OSError):
            tw.PipeTransport(inst)
    finally:
        t.close()
