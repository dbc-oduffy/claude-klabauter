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


def _local_open(tmp_path, local, far=()):
    record = roll_call.open_roll_call(RC, {"local": local, "far": list(far), "excluded": []}, "the ask", home=tmp_path)
    return roll_call.record_path(RC, tmp_path), record


def test_deliver_local_claude_appends_to_mailbox_and_others_route(tmp_path):
    local = [
        {"session_id": "c1", "name": "claude-em", "kind": "claude"},
        {"session_id": "x1", "name": "codex-em", "kind": "codex"},
        {"session_id": "x2", "name": "grok-em", "kind": "grok"},
        {"session_id": "x3", "name": "unbound", "kind": "agy"},
        {"session_id": "x4", "name": "refused-em", "kind": "codex"},
    ]
    _, record = _local_open(tmp_path, local)
    appended, routed, waited = [], [], []

    def route(msg):
        routed.append((msg.to_identity, msg.content))
        status = "refused" if msg.to_identity == "refused-em" else "handed-off"
        return types.SimpleNamespace(
            status=status, reason="loop" if status == "refused" else None, wait=lambda t=None: waited.append(msg.to_identity)
        )

    resolve = lambda name: None if name == "unbound" else {"kind": "codex"}  # noqa: E731
    out = roll_call_deliver.deliver_local(record, append=appended.append, route=route, resolve=resolve)

    assert [m.to_session for m in appended] == ["c1"] and appended[0].content == "the ask"
    assert out["c1"] == {"ok": True, "reason": None}
    assert out["x1"]["ok"] and out["x2"]["ok"]
    assert out["x3"] == {"ok": False, "reason": "no_binding"}
    assert out["x4"] == {"ok": False, "reason": "refused:loop"}
    assert [r[0] for r in routed] == ["codex-em", "grok-em", "refused-em"]
    assert waited == ["codex-em", "grok-em"]


def test_grok_row_name_from_the_registry_resolves_through_the_real_roster(tmp_path, monkeypatch):
    from coordinator_core.session import external_registry

    name = "repo-grok-box-1"
    row = {"provider": "grok", "session_id": "11111111-1111-4111-8111-111111111111", "name": name}
    monkeypatch.setattr(external_registry, "lookup", lambda key: None)
    monkeypatch.setattr(external_registry, "snapshot", lambda: {row["session_id"]: row})
    _, record = _local_open(tmp_path, [{"session_id": row["session_id"], "name": name, "kind": "grok"}])
    routed = []

    def route(msg):
        routed.append(msg.to_identity)
        return types.SimpleNamespace(status="delivered", reason=None, wait=lambda t=None: None)

    out = roll_call_deliver.deliver_local(record, route=route)

    assert out[row["session_id"]] == {"ok": True, "reason": None}
    assert routed == [name]


def test_deliver_local_failures_are_outcomes_not_raises():
    def append(msg):
        raise OSError("disk")

    local = [{"session_id": "c1", "name": "a"}, {"session_id": "c2", "name": "b"}]
    out = roll_call_deliver.deliver_local({"text": "t", "addressees": {"local": local}}, append=append)
    assert set(out) == {"c1", "c2"}
    assert all(not v["ok"] and "disk" in v["reason"] for v in out.values())


def test_deliver_records_one_row_per_local_far_and_unreachable_key(tmp_path):
    local = [{"session_id": "c1", "name": "a", "kind": "claude"}]
    far = [
        {"key": "b1:s1", "box_id": "b1", "name": "f"},
        {"key": "b2:s2", "box_id": "b2", "name": "g", "unreachable": True},
    ]
    path, _ = _local_open(tmp_path, local, far)
    sent = []

    def send(key, text, from_session):
        sent.append(key)
        return {"ok": True, "reason": None}

    result = roll_call_deliver.deliver(path, send=send, now=9.0, append=lambda m: None)

    assert sent == ["b1:s1"]
    deliveries = roll_call.load_record(RC, tmp_path)["deliveries"]
    assert set(deliveries) == {"c1", "b1:s1", "b2:s2"}
    assert deliveries["c1"]["ok"] is True
    assert deliveries["b2:s2"] == {"ok": False, "reason": "box_unreachable", "at": 9.0}
    assert result["sent"] == 3


def test_deliver_rerun_skips_delivered_local_and_retries_failed_local(tmp_path):
    local = [{"session_id": "c1", "name": "a"}, {"session_id": "c2", "name": "b"}]
    path, _ = _local_open(tmp_path, local)

    def flaky(msg):
        if msg.to_session == "c2":
            raise OSError("first")

    roll_call_deliver.deliver(path, send=lambda *a: {"ok": True}, append=flaky)
    again = []
    roll_call_deliver.deliver(path, send=lambda *a: {"ok": True}, append=again.append)
    assert [m.to_session for m in again] == ["c2"]
    assert all(v["ok"] for v in roll_call.load_record(RC, tmp_path)["deliveries"].values())


def test_unknown_record_is_a_reply_not_a_raise(tmp_path):
    missing = roll_call.record_path("rc-nope", tmp_path)
    assert roll_call_deliver.deliver(missing, send=lambda *a: {"ok": True}) == {
        "ok": False,
        "reason": "unknown_roll_call",
    }


def test_main_rejects_wrong_arity():
    assert roll_call_deliver.main([]) == 2


def test_crashing_worker_records_a_failure_for_every_undelivered_addressee(tmp_path, monkeypatch):
    local = [{"session_id": "c1", "name": "a", "kind": "claude"}]
    far = [{"key": "b1:s1", "box_id": "b1", "name": "f"}]
    path, _ = _local_open(tmp_path, local, far)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(roll_call_deliver, "deliver", boom)

    assert roll_call_deliver.main([str(path)]) == 1
    deliveries = roll_call.load_record(RC, tmp_path)["deliveries"]
    assert set(deliveries) == {"c1", "b1:s1"}
    assert all(not v["ok"] and "worker_failed:RuntimeError:kaboom" in v["reason"] for v in deliveries.values())


def test_worker_failure_keeps_keys_already_delivered_ok(tmp_path):
    local = [{"session_id": "c1", "name": "a", "kind": "claude"}, {"session_id": "c2", "name": "b", "kind": "claude"}]
    path, _ = _local_open(tmp_path, local)
    roll_call.record_deliveries(RC, {"c1": {"ok": True, "reason": None}}, home=tmp_path)

    roll_call_deliver.record_worker_failure(path, ValueError("x"), now=3.0)

    deliveries = roll_call.load_record(RC, tmp_path)["deliveries"]
    assert deliveries["c1"]["ok"] is True
    assert deliveries["c2"] == {"ok": False, "reason": "worker_failed:ValueError:x", "at": 3.0}


def test_record_worker_failure_on_missing_record_is_a_reply_not_a_raise(tmp_path):
    missing = roll_call.record_path("rc-nope", tmp_path)
    assert roll_call_deliver.record_worker_failure(missing, OSError("z")) == {
        "ok": False,
        "reason": "unknown_roll_call",
    }


def test_default_deliver_over_local_and_far_records_one_entry_per_addressee(tmp_path, monkeypatch):
    from coordinator_core.group_em import send_pass

    local = [{"session_id": "c1", "name": "a", "kind": "claude"}, {"session_id": "c2", "name": "b", "kind": "claude"}]
    far = [{"key": "b1:s1", "box_id": "b1", "name": "f"}, {"key": "b2:s2", "box_id": "b2", "name": "g"}]
    path, _ = _local_open(tmp_path, local, far)
    monkeypatch.setattr(send_pass, "send_to_peer", lambda key, text, frm: {"ok": True, "reason": None})
    from coordinator_core.agent_pipe import mailbox

    appended = []
    monkeypatch.setattr(mailbox, "append", appended.append)

    result = roll_call_deliver.deliver(path)

    assert result["sent"] == 4
    assert set(roll_call.load_record(RC, tmp_path)["deliveries"]) == {"c1", "c2", "b1:s1", "b2:s2"}
    assert [m.to_session for m in appended] == ["c1", "c2"]


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
