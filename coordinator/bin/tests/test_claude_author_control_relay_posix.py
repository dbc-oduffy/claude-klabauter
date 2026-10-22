"""POSIX PTY relay: bytes relay both ways, exit code propagates, hooks and timers run."""
import os
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty only")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

CHILD = (
    "import sys,os\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "line = sys.stdin.readline()\n"
    "sys.stdout.write('got:' + line.strip() + '\\n'); sys.stdout.flush()\n"
    "sys.stdout.write('size:%d\\n' % os.get_terminal_size(1).columns); sys.stdout.flush()\n"
    "sys.exit(7)\n"
)


class Obs:
    def __init__(self):
        self.inp = b""
        self.out = b""

    def on_input(self, data):
        self.inp += data

    def on_output(self, data):
        self.out += data


def test_relay_roundtrip_exit_code_and_hooks():
    from claude_author_control import protocol, relay_posix

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    obs = Obs()
    env = {"PATH": os.environ.get("PATH", ""), "MARK": "x"}
    relay = relay_posix.spawn([sys.executable, "-c", CHILD], env, obs,
                              stdin_fd=in_r, stdout_fd=out_w)
    assert isinstance(relay, protocol.Relay)
    assert relay.alive()
    relay.resize(97, 30)

    hook_r, hook_w = os.pipe()
    hits = []
    relay.register(hook_r, lambda: hits.append(os.read(hook_r, 16)))
    timer_hits = []
    relay.call_later(0.05, lambda: timer_hits.append(1))

    def feed():
        os.write(hook_w, b"h")
        os.write(in_w, b"hello\n")

    threading.Timer(0.3, feed).start()
    code = relay.run()
    os.close(out_w)
    relayed = b""
    while True:
        chunk = os.read(out_r, 65536)
        if not chunk:
            break
        relayed += chunk
    for fd in (in_r, in_w, out_r, hook_r, hook_w):
        os.close(fd)

    assert code == 7
    assert not relay.alive()
    assert b"hello" in obs.inp
    assert b"ready" in obs.out and b"got:hello" in obs.out
    assert b"got:hello" in relayed
    assert b"size:97" in obs.out
    assert hits == [b"h"]
    assert timer_hits == [1]
