"""The near mux client: a frame its local session cannot receive drops the far mirror, so the next send fails."""
import importlib.util
import io
import json
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inbox_fail_loud_t", BIN / "unimatrix-inbox.py")
inbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inbox)
inbox.log = io.StringIO()


class Pipe:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


def frames(pipe):
    return [json.loads(x) for x in pipe.data.splitlines() if x]


def test_failed_local_delivery_sends_a_drop_frame_and_parks_the_name(monkeypatch):
    pipe = Pipe()
    feed = inbox.MuxFeed(pipe, "unused", clock=lambda: 100.0)
    feed.sent["dc"] = (1, "ep")
    monkeypatch.setattr(inbox, "write_local", lambda *a: (_ for _ in ()).throw(OSError("session gone")))
    monkeypatch.setattr(inbox, "localize", lambda frame, line, box=None: line)
    inbox.deliver_frame({"type": "msg"}, b"{}\n", b"", "ep", "gipsy", on_fail=lambda e: feed.fail("dc"))
    assert frames(pipe) == [{"op": "drop", "name": "dc"}]
    assert "dc" not in feed.sent and feed.parked["dc"] > 100.0


def test_heartbeat_writes_the_ping_line_the_far_relay_leases_on():
    pipe = Pipe()
    assert inbox.MuxFeed(pipe, "unused").heartbeat() is True
    assert pipe.data == inbox.PING_LINE


def test_a_session_that_exits_is_dropped_on_the_next_poll_before_any_send(tmp_path, monkeypatch):
    """Window bound: MUX_POLL_SECONDS. The far socket is unlinked on the poll after the process dies."""
    pipe = Pipe()
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"dc": {"pid": 7, "endpoint": "ep"}}))
    live = {7: True}
    monkeypatch.setattr(inbox, "mux_add_frame", lambda n, pid, ep: {"op": "add", "name": n, "entry": {"procStart": "t1"}, "key": "k"})
    feed = inbox.MuxFeed(pipe, str(names), clock=lambda: 100.0, alive=lambda pid, ps: live[pid] and ps == "t1")
    assert feed.reconcile() and [f["op"] for f in frames(pipe)] == ["add"]
    assert feed.reconcile() and [f["op"] for f in frames(pipe)] == ["add"]  # still alive: no drop
    live[7] = False
    assert feed.reconcile()
    assert [f["op"] for f in frames(pipe)] == ["add", "drop"] and "dc" not in feed.sent
    assert feed.reconcile() and [f["op"] for f in frames(pipe)] == ["add", "drop"]  # parked: not re-added


def test_mux_poll_interval_bounds_the_residual_window():
    assert inbox.MUX_POLL_SECONDS <= 2
