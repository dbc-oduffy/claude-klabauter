"""Tests for coordinator/bin/lib/claude_author_control/protocol.py (protocol v0 codec)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from claude_author_control import protocol as p  # noqa: E402


def _line(**kw) -> bytes:
    obj = {"v": 0, "id": 1, "op": "status", "args": {}, "on_behalf_of": None}
    obj.update(kw)
    return json.dumps(obj).encode()


VALID_ARGS = {
    "prompt": {"text": "hello", "force": True},
    "compact": {"instructions": "keep tests", "force": False},
    "model": {"name": "opus"},
    "effort": {"level": "high"},
    "interrupt": {},
    "status": {},
}


def test_init_has_no_imports():
    src = (Path(p.__file__).parent / "__init__.py").read_text()
    assert "import " not in src


@pytest.mark.parametrize("op", p.OPS)
def test_every_op_round_trips(op):
    req = p.parse_request(_line(op=op, args=VALID_ARGS[op], id="abc", on_behalf_of="PM"))
    assert isinstance(req, p.Request)
    assert (req.op, req.id, req.args, req.on_behalf_of) == (op, "abc", VALID_ARGS[op], "PM")
    resp = json.loads(p.encode_response(p.ok_response(req.id, "injected", {"a": 1})))
    assert resp == {"v": 0, "id": "abc", "ok": True, "state": "injected", "identity": {"a": 1}}


def test_status_response_has_status_and_no_state():
    resp = p.ok_response(3, status={"alive": True})
    assert "state" not in resp and resp["status"] == {"alive": True}


def test_constants():
    assert set(p.ERROR_CODES) == {"busy_user", "unknown_op", "bad_args", "not_attested",
                                  "pty_dead"}
    assert "deferred_idle" in p.STATES and "deferred_idle" in p.RESERVED_STATES
    assert "deferred_idle" not in p.EMITTED_STATES
    assert p.MAX_FRAME_BYTES == 1 << 20


def _code(r):
    assert isinstance(r, p.ErrorResponse)
    return r.code


def test_malformed_json_bad_args_null_id():
    r = p.parse_request(b"{nope")
    assert _code(r) == "bad_args" and r.id is None
    assert json.loads(p.encode_response(r))["id"] is None


def test_wrong_version():
    assert _code(p.parse_request(_line(v=1))) == "bad_args"
    assert p.parse_request(_line(v=1)).id is None


def test_non_object_and_bad_id():
    assert _code(p.parse_request(b"[1]")) == "bad_args"
    assert _code(p.parse_request(_line(id=None))) == "bad_args"
    assert _code(p.parse_request(_line(id=True))) == "bad_args"


def test_unknown_op_keeps_id():
    r = p.parse_request(_line(op="reboot", id=9))
    assert _code(r) == "unknown_op" and r.id == 9


@pytest.mark.parametrize("op,args", [
    ("prompt", {}), ("model", {}), ("effort", {}), ("prompt", {"text": ""}),
    ("prompt", {"text": "x", "bogus": 1}), ("prompt", {"text": "x", "force": "yes"}),
    ("status", {"x": 1}), ("model", {"name": 5}),
])
def test_missing_or_bad_args(op, args):
    assert _code(p.parse_request(_line(op=op, args=args))) == "bad_args"


def test_args_not_object_and_bad_on_behalf_of():
    assert _code(p.parse_request(_line(args=[1]))) == "bad_args"
    assert _code(p.parse_request(_line(on_behalf_of="root"))) == "bad_args"


@pytest.mark.parametrize("op,args", [
    ("model", {"name": "op\nus"}), ("model", {"name": "o\x1bus"}),
    ("effort", {"level": "hi\rgh"}), ("compact", {"instructions": "a\nb"}),
    ("compact", {"instructions": "a\x03b"}), ("prompt", {"text": "a\x1b[201~b"}),
])
def test_control_chars_rejected(op, args):
    assert _code(p.parse_request(_line(op=op, args=args))) == "bad_args"


def test_prompt_text_may_contain_newline_and_tab():
    assert isinstance(p.parse_request(_line(op="prompt", args={"text": "a\n\tb"})), p.Request)


def test_over_length_line_closes():
    r = p.parse_request(b"x" * (p.MAX_FRAME_BYTES + 1))
    assert _code(r) == "bad_args" and r.close and r.id is None


def test_line_splitter():
    s = p.LineSplitter()
    assert s.feed(b'{"a":1}\n{"b"') == [b'{"a":1}']
    assert s.feed(b":2}\n") == [b'{"b":2}']
    with pytest.raises(p.ProtocolError) as e:
        s.feed(b"y" * (p.MAX_FRAME_BYTES + 1))
    assert e.value.code == "bad_args"


def test_handle_line_unattested_and_handler():
    out, close = p.handle_line(_line(id=4), None, lambda r, peer: {})
    resp = json.loads(out)
    assert resp["error"]["code"] == "not_attested" and resp["id"] == 4 and not close
    peer = p.Peer("unix", uid=5, pid=None)
    out, _ = p.handle_line(_line(id=4), peer,
                           lambda r, pe: p.ok_response(r.id, identity=p.identity_for(pe)))
    assert json.loads(out)["identity"] == {"transport": "unix", "uid": 5, "pid": None}
    out, _ = p.handle_line(_line(id=4), peer,
                           lambda r, pe: (_ for _ in ()).throw(p.ProtocolError("pty_dead", "x")))
    assert json.loads(out)["error"]["code"] == "pty_dead"
    out, close = p.handle_line(b"junk", peer, lambda r, pe: {})
    assert json.loads(out)["error"]["code"] == "bad_args" and not close


def test_error_response_rejects_unknown_code():
    with pytest.raises(ValueError):
        p.error_response(1, "nope", "x")


def test_identity_envelope():
    unix = p.Peer("unix", uid=1, pid=2)
    assert p.identity_for(unix, "PM") == {"transport": "unix", "uid": 1, "pid": 2,
                                          "on_behalf_of": "PM", "relayed": True}
    win = p.Peer("pipe", pid=7, sid="S-1-5-21")
    assert p.identity_for(win) == {"transport": "pipe", "pid": 7, "sid": "S-1-5-21"}


# ---- keystroke encoding (relayed=False isolates the documented bytes)

def test_encode_interrupt_and_status():
    assert p.encode_op("interrupt", {}, False) == b"\x1b"
    assert p.encode_op("status", {}, False) == b""


def test_encode_slash_commands():
    assert p.encode_op("compact", {}, False) == b"/compact\r"
    assert p.encode_op("compact", {"instructions": "keep x"}, True) == b"/compact keep x\r"
    assert p.encode_op("model", {"name": "opus"}, True) == b"/model opus\r"
    assert p.encode_op("effort", {"level": "high"}, False) == b"/effort high\r"


def test_encode_prompt_paste_and_plain():
    assert p.encode_op("prompt", {"text": "hi"}, True, relayed=False) == \
        b"\x1b[200~hi\x1b[201~\r"
    assert p.encode_op("prompt", {"text": "hi"}, False, relayed=False) == b"hi\r"
    assert p.encode_op("prompt", {"text": "a\nb"}, True, relayed=False) == \
        b"\x1b[200~a\nb\x1b[201~\r"


def test_multiline_without_paste_mode_is_bad_args():
    with pytest.raises(p.ProtocolError) as e:
        p.encode_op("prompt", {"text": "a\nb"}, False)
    assert e.value.code == "bad_args"


def test_encode_op_rejects_control_chars_and_unknown():
    for op, args in (("model", {"name": "a\x1b"}), ("effort", {"level": "a\n"}),
                     ("compact", {"instructions": "a\r"})):
        with pytest.raises(p.ProtocolError) as e:
            p.encode_op(op, args, True)
        assert e.value.code == "bad_args"
    with pytest.raises(p.ProtocolError) as e:
        p.encode_op("nope", {}, True)
    assert e.value.code == "unknown_op"


def test_force_prefix():
    assert p.FORCE_PREFIX == b"\x1b"


def test_relayed_marker_with_and_without_on_behalf_of():
    assert p.render_relayed("hi") == "[relayed via control-socket] hi"
    assert p.render_relayed("hi", "PM") == "[relayed via control-socket on behalf of PM] hi"
    assert p.encode_op("prompt", {"text": "hi"}, False) == \
        b"[relayed via control-socket] hi\r"
    assert p.encode_op("prompt", {"text": "hi"}, False, on_behalf_of="PM") == \
        b"[relayed via control-socket on behalf of PM] hi\r"
    assert b"relayed" not in p.encode_op("model", {"name": "opus"}, True)


# ---- shapes and seams

def test_turn_and_discovery_shapes():
    t = p.turn_record("sid", True, "Stop", 1.0)
    assert tuple(t) == p.TURN_FIELDS and p.TURN_FILE == "turn.json" and p.SOCK_NAME == "sock"
    d = p.discovery_record(instance="a" * 12, session_id=None, transport="unix", address="/x",
                           supervisor_pid=1, child_pid=2, cwd="/c", started_at="t")
    assert tuple(d) == p.DISCOVERY_FIELDS
    assert p.is_instance_id("0123456789ab") and not p.is_instance_id("xyz")
    assert p.pipe_address("abc") == "\\\\.\\pipe\\claude-author-abc"
    assert p.MAX_SOCK_PATH_BYTES == 103


def test_protocol_classes_structural():
    class Obs:
        def on_input(self, d): ...
        def on_output(self, d): ...

    class R:
        def write(self, data): ...
        def resize(self, cols, rows): ...
        def alive(self): return True
        def wait(self): return 0

    class T:
        address = "x"
        def serve(self, handler): ...
        def close(self): ...

    assert isinstance(Obs(), p.Observers)
    assert isinstance(R(), p.Relay)
    assert isinstance(T(), p.Transport)
    assert not isinstance(object(), p.Relay)


def test_no_os_leg_imports():
    src = Path(p.__file__).read_text()
    for banned in ("import os", "import socket", "import ctypes", "import pty", "import termios"):
        assert banned not in src
