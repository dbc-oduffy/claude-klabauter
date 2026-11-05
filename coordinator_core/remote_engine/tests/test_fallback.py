"""Tests for remote_engine.fallback.try_remote."""

import json
import subprocess

from coordinator_core.remote_engine import fallback

SSH = {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}


def _box(bid, transport=SSH):
    rec = {"box_id": bid, "root": "/r", "capability": {}}
    if transport is not None:
        rec["transport"] = transport
    return rec


class Runner:
    def __init__(self, stdout='{"result": {"rows": []}}', rc=0):
        self.calls = []
        self.stdout, self.rc = stdout, rc

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(fallback.box_transport, "_registered", lambda box, host, invoke: None)
    fallback.box_transport._reset_limits()
    monkeypatch.delenv(fallback.HOOK_ENV, raising=False)
    monkeypatch.setattr(fallback, "telemetry_path", lambda: tmp_path / "t.jsonl")


def test_serves_once_excluding_local(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    r = Runner()
    out = fallback.try_remote(
        "session.peer_roster", {}, runner=r, boxes=[_box("a"), _box("b")], local_id="a"
    )
    assert len(r.calls) == 1
    assert out["served_by"] == "b" and out["remote"] is True
    assert out["latency_ms"] >= 0 and out["reply"] == {"rows": []}
    assert r.calls[0][1] == fallback.DEFAULT_REMOTE_TIMEOUT_S
    line = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert line["served_by"] == "b" and line["ok"] is True


def test_explicit_timeout_passed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    r = Runner()
    fallback.try_remote("x", {}, timeout=2.5, runner=r, boxes=[_box("b")], local_id="a")
    assert r.calls[0][1] == 2.5


def test_refuses_not_remote_safe(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = Runner()
    assert fallback.try_remote("trim-file-cache", {}, runner=r, boxes=[_box("b")], local_id="a") is None
    assert r.calls == []


def test_refuses_in_hook_context(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    monkeypatch.setenv(fallback.HOOK_ENV, "0")
    r = Runner()
    assert fallback.try_remote("x", {}, runner=r, boxes=[_box("b")], local_id="a") is None
    assert r.calls == []
    monkeypatch.setenv(fallback.HOOK_ENV, "5")
    assert fallback.try_remote("x", {}, runner=r, boxes=[_box("b")], local_id="a") is not None


def test_no_ssh_box_returns_none(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    r = Runner()
    boxes = [_box("a"), _box("b", transport=None), _box("c", {"kind": "fs"})]
    assert fallback.try_remote("x", {}, runner=r, boxes=boxes, local_id="a") is None
    assert r.calls == []


def test_transport_failure_returns_none_and_records(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    r = Runner(rc=255)
    assert fallback.try_remote("x", {}, runner=r, boxes=[_box("b")], local_id="a") is None
    assert len(r.calls) == 1
    line = json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])
    assert line["ok"] is False


def test_timeout_returns_none(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)

    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert fallback.try_remote("x", {}, runner=boom, boxes=[_box("b")], local_id="a") is None


def test_phase2_timeout_returns_indeterminate_not_none(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    out = fallback.try_remote("dispatch.emit", {"a": 1}, runner=boom, boxes=[_box("b")], local_id="a")
    assert out["indeterminate"] is True and out["served_by"] == "b"
    assert out["request_key"] is None and "dispatch.emit" in out["reason"]


def test_phase2_far_error_key_is_carried(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = {"ok": False, "reason": "remote_error", "error": {"code": -32004, "data": {"dispatch_key": "k-1"}}}
    monkeypatch.setattr(fallback.box_transport, "invoke_remote", lambda *a, **k: res)
    out = fallback.try_remote("hooks.submit_engine_ask", {}, boxes=[_box("b")], local_id="a")
    assert out["indeterminate"] is True and out["request_key"] == "k-1"


def test_phase2_exception_after_send_is_indeterminate(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def raising(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(fallback.box_transport, "invoke_remote", raising)
    out = fallback.try_remote("dispatch.emit", {}, boxes=[_box("b")], local_id="a")
    assert out["indeterminate"] is True
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    assert fallback.try_remote("x", {}, boxes=[_box("b")], local_id="a") is None


def test_phase2_success_is_a_normal_reply(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = fallback.try_remote("dispatch.emit", {}, runner=Runner(), boxes=[_box("b")], local_id="a")
    assert "indeterminate" not in out and out["remote"] is True


def test_local_path_params_refused_before_send(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = Runner()
    for params in ({"cwd": "/abs/tree"}, {"repo_root": "E:\\repo"}):
        assert fallback.try_remote("dispatch.emit", params, runner=r, boxes=[_box("b")], local_id="a") is None
    assert r.calls == []
    assert fallback.try_remote("hooks.submit_engine_ask", {"text": "/compact now"}, runner=r,
                               boxes=[_box("b")], local_id="a") is not None


def test_explicit_hook_marker_refuses(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(fallback.policy, "remote_safe", lambda op: True)
    monkeypatch.setenv(fallback.HOOK_MARKER_ENV, "1")
    r = Runner()
    assert fallback.try_remote("x", {}, runner=r, boxes=[_box("b")], local_id="a") is None
    assert r.calls == []
    monkeypatch.setenv(fallback.HOOK_MARKER_ENV, "0")
    assert fallback.try_remote("x", {}, runner=r, boxes=[_box("b")], local_id="a") is not None
