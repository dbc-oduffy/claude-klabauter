"""The far relay releases a mirror host whose near client stopped pinging (portable: select and read are faked)."""
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("relay_lease_t", BIN / "unimatrix-relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def run_mux(monkeypatch, chunks, lease=90):
    """Drive serve_mux: each select call pops a chunk (bytes -> stdin ready) or None (timeout)."""
    timeouts, script = [], list(chunks)

    monkeypatch.setattr(relay.os, "read", lambda fd, n: script.pop(0))

    def sel(r, w, x, timeout=None):
        timeouts.append(timeout)
        if script and script[0] is None:
            script.pop(0)
            return [], [], []
        if not script:
            raise AssertionError("serve_mux kept waiting after the lease should have lapsed")
        return [7], [], []
    monkeypatch.setattr(relay.select, "select", sel)
    relay.serve_mux(7, None, lease=lease, clock=lambda: 1000.0)
    return timeouts


def test_a_pinged_mux_returns_when_the_next_wait_times_out(monkeypatch):
    t = run_mux(monkeypatch, [relay.PING_LINE, None])
    assert t[0] is None and t[1] == 90  # unarmed waits forever; armed waits one lease


def test_a_mux_that_never_pinged_waits_without_a_timeout(monkeypatch):
    t = run_mux(monkeypatch, [b"", ])
    assert t == [None]


def test_single_inbox_serve_returns_when_the_lease_lapses(monkeypatch):
    script = [relay.PING_LINE, None]
    seen = []

    def sel(r, w, x, timeout=None):
        seen.append(timeout)
        if script[0] is None:
            return [], [], []
        return [9], [], []
    monkeypatch.setattr(relay.select, "select", sel)
    monkeypatch.setattr(relay.os, "read", lambda fd, n: script.pop(0))
    relay.serve("srv", 9, None, name="dc", lease=90, clock=lambda: 5.0)
    assert seen == [None, 90]
