"""Behavior pins for the vendored unimatrix mirror-all, roster and reshape scripts."""
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import CompletedProcess
    from types import ModuleType

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]
NAMES = ("mirror-all", "roster", "reshape")
FAR_SOCK = "/tmp/cc-socks/4242.sock"


FAKE_DNS = {"farbox": "10.9.0.5", "other": "10.9.0.6"}


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in FAKE_DNS:
            raise OSError("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_DNS[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def near_box_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """This box sorts before every far box in these tests, so it owns the mirror unless a test says otherwise."""
    monkeypatch.setattr(socket, "gethostname", lambda: "aaa-near")


LIVE = {"a": {"pid": 11, "name": "a", "messagingSocketPath": "ep11"}}


IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
USER_AT_HOST = re.compile(r"\b[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*\b")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handler is POSIX-only")
@pytest.mark.parametrize("name", ["roster", "mirror-all"])
def test_a_sigterm_stop_is_logged_and_exits_nonzero(tmp_path: Path, name) -> None:
    # A clean 0 on SIGTERM dropped every mirror with no trace; the stop must say so and fail.
    log = tmp_path / "sup.log"
    p = subprocess.Popen([sys.executable, str(BIN / f"unimatrix-{name}.py"), str(log),
                          "--remote", "unreachable.invalid", "--key", str(tmp_path / "k")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env={**os.environ, "UNIMATRIX_ROUTER_SUPERVISION": "off"})
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not log.exists():
            time.sleep(0.05)
        time.sleep(1)  # the handler is installed just after the log opens
        p.terminate()
        assert p.wait(timeout=10) == 143
        assert "stopped by signal 15" in log.read_text(encoding="utf-8")
    finally:
        # The supervisor polls forever: a failed or interrupted assertion must never leave it running.
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
