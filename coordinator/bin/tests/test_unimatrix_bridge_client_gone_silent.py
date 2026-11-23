"""A client that hangs up before the bridge's verdict (a liveness probe) is not logged as a fault; a real write failure still is."""
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


class Conn:
    def __init__(self, data, write_exc):
        self.chunks = [data, b""]
        self.write_exc = write_exc

    def read(self):
        return self.chunks.pop(0)

    def write(self, data):
        raise self.write_exc

    def close(self):
        pass


@pytest.fixture
def bridge(tmp_path):
    spec = importlib.util.spec_from_file_location("unimatrix_bridge_client_gone", BIN / "unimatrix-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = open(tmp_path / "bridge.log", "a+", encoding="utf-8")
    yield mod, tmp_path / "bridge.log"
    mod.log.close()


def run(mod, path, exc):
    mod.relay_send(Conn(b"", exc))  # empty buffer: the "nothing to send" verdict
    mod.log.flush()
    return path.read_text(encoding="utf-8")


def test_broken_pipe_is_silent(bridge):
    mod, path = bridge
    assert "verdict not delivered" not in run(mod, path, BrokenPipeError(32, "Broken pipe"))


def test_other_write_failure_is_logged(bridge):
    mod, path = bridge
    assert "verdict not delivered" in run(mod, path, PermissionError("denied"))
