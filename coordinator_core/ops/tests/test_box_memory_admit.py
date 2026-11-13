"""box_memory.admit/register/release: engine-side admission over a fake host, dispatched through the IPC chokepoint."""
import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from coordinator_core import ipc
from coordinator_core.bash_guards import _heavy_command_class as hcc
from coordinator_core.ops import box_memory_admit  # noqa: F401  (registers the ops)

OPS = ("box_memory.admit", "box_memory.register", "box_memory.release")
GIB = 1024 ** 3


@dataclasses.dataclass
class _Status:
    avail_phys: int
    commit_used: int = 0


class _Host:
    GIB = GIB

    def __init__(self, avail_gib, reserved_gib=0.0):
        self.status = _Status(int(avail_gib * GIB))
        self.reserved = int(reserved_gib * GIB)
        self.rows = {}
        self.locked = 0

    def admission_file(self):
        return "fake-admission"

    def read_recorded_peak(self, path, key):
        return None

    def read_memory_status(self):
        return self.status

    def admission_lock(self, path):
        host = self

        class _Lock:
            def __enter__(self):
                host.locked += 1

            def __exit__(self, *a):
                return False

        return _Lock()

    def reserved_bytes(self, path):
        return self.reserved

    def admit(self, peak_bytes, status):
        return SimpleNamespace(admit=status.avail_phys >= peak_bytes, need_avail=peak_bytes)

    def top_holders(self, path, n):
        return []

    def register_command(self, path, key, pid, peak_gib):
        self.rows[(key, pid)] = peak_gib

    def release_command(self, path, key, pid):
        self.rows.pop((key, pid), None)


@pytest.fixture
def host(monkeypatch):
    h = _Host(avail_gib=8)
    monkeypatch.setattr(hcc, "_load_box_memory_host", lambda: h)
    return h


def _call(method, params):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return asyncio.run(ipc.dispatch_message(msg))


def test_every_op_is_registered_in_every_registry():
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.op_scopes import _OP_KEY_SCOPE
    from coordinator_core.ops import _EAGER_OP_MODULES
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    eager = {m for m, _ in _EAGER_OP_MODULES}
    for op in OPS:
        assert op in ipc._REGISTRY
        assert OP_MODULE_MAP[op] == "coordinator_core.ops.box_memory_admit"
        assert OP_MODULE_MAP[op] in eager
        assert _OP_KEY_SCOPE[op] == "none"
        assert OP_CLASSIFICATION[op] is OpClass.MUTATING


def test_admit_returns_the_measured_verdict_and_writes_the_reservation(host):
    d = _call("box_memory.admit", {"key": "k", "default_peak_gib": 1.0, "register_pid": 9})
    assert "error" not in d
    r = d["result"]
    assert r["admitted"] is True and r["measured"] is True
    assert r["key"] == "k" and r["peak_gib"] == 1.0 and r["reason"].startswith("ADMIT k")
    assert host.rows == {("k", 9): 1.0}
    assert host.locked == 1


def test_admit_without_register_pid_writes_nothing(host):
    r = _call("box_memory.admit", {"key": "k", "default_peak_gib": 1.0})["result"]
    assert r["admitted"] is True and host.rows == {}


def test_admit_refuses_with_a_hold_when_headroom_is_short_and_writes_nothing(host):
    host.reserved = int(7.5 * GIB)
    r = _call("box_memory.admit", {"key": "k", "default_peak_gib": 4.0, "register_pid": 9})["result"]
    assert r["admitted"] is False and r["measured"] is True and r["reason"].startswith("HOLD k")
    assert host.rows == {}


def test_admit_never_waits_for_a_slot(host, monkeypatch):
    host.status = _Status(0)
    monkeypatch.setattr(hcc, "_wait_for_slot", lambda *a, **k: pytest.fail("slot wait inside the engine"))
    r = _call("box_memory.admit", {"key": "k", "default_peak_gib": 64.0, "register_pid": 9, "session_id": "s"})["result"]
    assert r["admitted"] is False


def test_a_host_permission_error_is_an_error_envelope_not_an_admit(host, monkeypatch):
    def boom():
        raise PermissionError("denied")

    monkeypatch.setattr(host, "read_memory_status", boom)
    d = _call("box_memory.admit", {"key": "k", "default_peak_gib": 1.0, "register_pid": 9})
    assert "error" in d and "result" not in d
    assert host.rows == {}


@pytest.mark.parametrize("params", [
    {"default_peak_gib": 1.0},
    {"key": "k"},
    {"key": "k", "default_peak_gib": 1.0, "extra": 1},
    {"key": "k", "default_peak_gib": "1"},
    {"key": "k", "default_peak_gib": 1.0, "register_pid": True},
    {"key": "k", "default_peak_gib": 0},
])
def test_admit_rejects_malformed_params(host, params):
    d = _call("box_memory.admit", params)
    assert "error" in d and host.rows == {}


def test_register_then_release_round_trip(host):
    assert _call("box_memory.register", {"key": "k", "peak_gib": 2.0, "pid": 5})["result"] == {"ok": True}
    assert host.rows == {("k", 5): 2.0}
    assert _call("box_memory.release", {"key": "k", "pid": 5})["result"] == {"ok": True}
    assert host.rows == {}


def test_register_reports_a_write_failure_as_not_ok(host, monkeypatch):
    def fail(*a):
        raise OSError("disk")

    monkeypatch.setattr(host, "register_command", fail)
    assert _call("box_memory.register", {"key": "k", "peak_gib": 2.0, "pid": 5})["result"] == {"ok": False}


def test_release_of_an_unwritable_file_still_reports_ok(host, monkeypatch):
    def fail(*a):
        raise OSError("disk")

    monkeypatch.setattr(host, "release_command", fail)
    assert _call("box_memory.release", {"key": "k", "pid": 5})["result"] == {"ok": True}


@pytest.fixture
def dials(monkeypatch):
    calls, rows = [], []
    monkeypatch.setattr(hcc, "_engine_call", lambda method, params: calls.append(method))
    from coordinator_core.warm import telemetry

    monkeypatch.setattr(telemetry, "record_sandbox_advisory_admit", lambda **kw: rows.append(kw))
    return calls, rows


def _deny(*a):
    raise PermissionError("sandbox denied")


def test_in_engine_admit_with_a_denied_reservation_write_never_dials_the_engine(host, monkeypatch, dials):
    calls, rows = dials
    monkeypatch.setattr(host, "register_command", _deny)
    r = _call("box_memory.admit", {"key": "k", "default_peak_gib": 1.0, "register_pid": 9})["result"]
    assert r["admitted"] is False
    assert calls == []
    assert rows == [{"key": "k", "pid": 9}]


def test_in_engine_register_and_release_permission_errors_never_dial_the_engine(host, monkeypatch, dials):
    calls, rows = dials
    monkeypatch.setattr(host, "register_command", _deny)
    monkeypatch.setattr(host, "release_command", _deny)
    with hcc.in_engine():
        assert hcc.register_spawn("k", 1.0, 5, host) is False
        hcc.release_spawn("k", 5, host)
    assert calls == []
    assert len(rows) == 2


def test_in_engine_admit_before_spawn_takes_the_advisory_arm(host, monkeypatch, dials):
    calls, rows = dials
    monkeypatch.setattr(host, "read_memory_status", _deny)
    with hcc.in_engine():
        v = hcc.admit_before_spawn("k", 1.0, host, 9)
    assert v.admitted and "advisory" in v.reason
    assert calls == []
    assert rows == [{"key": "k", "pid": 9}]


def test_outside_the_engine_the_permission_error_arms_still_dial(host, monkeypatch, dials):
    calls, rows = dials
    monkeypatch.setattr(host, "register_command", _deny)
    monkeypatch.setattr(host, "release_command", _deny)
    monkeypatch.setattr(host, "read_memory_status", _deny)
    hcc.register_spawn("k", 1.0, 5, host)
    hcc.release_spawn("k", 5, host)
    hcc.admit_before_spawn("k", 1.0, host, 9)
    assert calls == ["box_memory.register", "box_memory.release", "box_memory.admit"]
