"""Tests for the roll-call far-delivery worker (injected sender and Popen; nothing spawns)."""

from __future__ import annotations

import subprocess
import sys
import types

from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import roll_call, roll_call_deliver

RC = "rc-test-1"


def _open(tmp_path, far, **extra):
    addressees = {"local": [], "far": far, "excluded": []}
    record = roll_call.open_roll_call(RC, addressees, "the ask", home=tmp_path)
    path = roll_call.record_path(RC, tmp_path)
    if extra:
        record.update(extra)
        roll_call.atomic_record.write_json_atomic(path, record)
    return path


def test_each_far_key_gets_one_send_and_outcome_lands_in_record(tmp_path):
    far = [{"key": "b1:s1", "box_id": "b1", "name": "a"}, {"key": "b2:s2", "box_id": "b2", "name": "b"}]
    path = _open(tmp_path, far)
    calls = []

    def send(key, text, from_session):
        calls.append((key, text, from_session))
        return {"ok": key == "b1:s1", "reason": None if key == "b1:s1" else "unknown_box"}

    result = roll_call_deliver.deliver(path, send=send, now=5.0)

    assert result["sent"] == 2
    assert calls == [("b1:s1", "the ask", None), ("b2:s2", "the ask", None)]
    deliveries = roll_call.load_record(RC, tmp_path)["deliveries"]
    assert deliveries["b1:s1"] == {"ok": True, "reason": None, "at": 5.0}
    assert deliveries["b2:s2"]["ok"] is False and deliveries["b2:s2"]["reason"] == "unknown_box"


def test_rerun_skips_delivered_and_retries_failed(tmp_path):
    far = [{"key": "b1:s1", "box_id": "b1", "name": "a"}, {"key": "b2:s2", "box_id": "b2", "name": "b"}]
    path = _open(tmp_path, far)
    roll_call_deliver.deliver(
        path, send=lambda k, t, f: {"ok": k == "b1:s1", "reason": "x"}
    )
    calls = []
    roll_call_deliver.deliver(path, send=lambda k, t, f: calls.append(k) or {"ok": True})
    assert calls == ["b2:s2"]
    assert all(v["ok"] for v in roll_call.load_record(RC, tmp_path)["deliveries"].values())


def test_a_raising_sender_is_recorded_and_the_rest_still_send(tmp_path):
    far = [{"key": "b1:s1", "box_id": "b1", "name": "a"}, {"key": "b2:s2", "box_id": "b2", "name": "b"}]
    path = _open(tmp_path, far)

    def send(key, text, from_session):
        if key == "b1:s1":
            raise RuntimeError("boom")
        return {"ok": True}

    roll_call_deliver.deliver(path, send=send)
    deliveries = roll_call.load_record(RC, tmp_path)["deliveries"]
    assert deliveries["b1:s1"]["ok"] is False and "boom" in deliveries["b1:s1"]["reason"]
    assert deliveries["b2:s2"]["ok"] is True


def test_unknown_record_is_a_reply_not_a_raise(tmp_path):
    missing = roll_call.record_path("rc-nope", tmp_path)
    assert roll_call_deliver.deliver(missing, send=lambda *a: {"ok": True}) == {
        "ok": False,
        "reason": "unknown_roll_call",
    }


def test_main_rejects_wrong_arity():
    assert roll_call_deliver.main([]) == 2


def test_detach_kwargs_per_platform():
    nt = roll_call_deliver.detach_kwargs("nt")
    assert nt["creationflags"] & 0x08000000 and not nt["creationflags"] & 0x00000008
    assert roll_call_deliver.detach_kwargs("posix") == {"start_new_session": True}


def _stub_capacity(monkeypatch):
    monkeypatch.setattr(capacity, "_load_box_memory_host", lambda: object())
    monkeypatch.setattr(capacity, "register_spawn", lambda *a, **k: True)
    monkeypatch.setattr(capacity, "release_spawn", lambda *a, **k: None)


def test_launch_starts_one_closed_argv_child_and_does_not_wait(tmp_path, monkeypatch):
    _stub_capacity(monkeypatch)
    seen = {}

    def popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return types.SimpleNamespace(pid=4242)

    admit = lambda key, peak, **kw: types.SimpleNamespace(admitted=True, reason="", peak_gib=peak)
    path = tmp_path / "roll-call" / "rc-x.json"

    result = roll_call_deliver.launch(path, popen=popen, admit=admit)

    assert result == {"launched": True, "pid": 4242}
    assert seen["argv"] == [sys.executable, "-m", "coordinator_core.group_em.roll_call_deliver", str(path)]
    assert seen["kwargs"].get("shell") is None
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL


def test_launch_held_admission_spawns_nothing(tmp_path, monkeypatch):
    _stub_capacity(monkeypatch)

    def popen(*a, **k):
        raise AssertionError("must not spawn on a held admission")

    admit = lambda key, peak, **kw: types.SimpleNamespace(admitted=False, reason="HOLD", peak_gib=peak)
    result = roll_call_deliver.launch(tmp_path / "x.json", popen=popen, admit=admit)
    assert result["launched"] is False and "HOLD" in result["reason"]


def test_launch_spawn_failure_is_a_reply(tmp_path, monkeypatch):
    _stub_capacity(monkeypatch)

    def popen(*a, **k):
        raise OSError("nope")

    admit = lambda key, peak, **kw: types.SimpleNamespace(admitted=True, reason="", peak_gib=peak)
    result = roll_call_deliver.launch(tmp_path / "x.json", popen=popen, admit=admit)
    assert result["launched"] is False and "nope" in result["reason"]
