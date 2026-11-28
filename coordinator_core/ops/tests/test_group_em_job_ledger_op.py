"""
coordinator_core.ops.tests.test_group_em_job_ledger_op -- the fleet job ledger through the registered
handlers, over a tmp settings home holding booked grants and an injected process census.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import box_registry, box_transport, capacity_admit, fleet_live, proc_census
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_fleet_capacity  # noqa: F401 -- registers groupem.job_progress
from coordinator_core.ops import group_em_job_ledger as op

_NOW = time.time()


def _call(name: str, params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": f"groupem.{name}", "params": params}
    out = asyncio.run(ipc.dispatch_message(msg))
    assert out.get("result") is not None, out
    return out["result"]


def _proc(pid: int, rss_mb: float = 4096.0, cmd: str = "worker") -> dict:
    return {"pid": pid, "ppid": 1, "comm": cmd, "cmdline": cmd, "start": _NOW - 300, "cpu_s": 1.0,
            "rss_mb": rss_mb, "io_read_bytes": 0, "io_write_bytes": 0}


def _grant(run: str, pid: int, granted_ago: float, window_s: float, **extra) -> dict:
    return {"run": run, "pid": pid, "owner_sid": f"sid-{run}", "granted_at": time.time() - granted_ago,
            "window_s": window_s, **extra}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(h))
    box_registry.ensure_identity(h)
    monkeypatch.setattr(box_transport, "invoke_remote", _far_unreachable)
    return h


def _far_unreachable(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
    return {"ok": False, "state": "unknown", "reason": "ssh_unreachable"}


def _seed(home: Path, monkeypatch: pytest.MonkeyPatch, procs: list, grants: list) -> None:
    path = capacity_admit.grants_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(g) + "\n" for g in grants), encoding="utf-8")
    real = proc_census.read
    monkeypatch.setattr(
        proc_census, "read",
        lambda readers=None, system=None, now=None: real(
            {"processes": lambda: procs, "vram": lambda: {"gpu_unavailable": "none"}}, "Linux", now),
    )


_REAL_INVOKE = box_transport.invoke_remote


def test_far_box_leg_is_admitted_by_the_remote_safe_gate_and_returns_rows(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    from coordinator_core.remote_engine import policy

    assert policy.remote_safe("groupem.job_ledger_local") and policy.remote_safe("groupem.job_ledger")
    assert not policy.remote_safe("groupem.job_progress")  # MUTATING: the booker records progress on its own box

    invoke_path = "/opt/far/bin/coordinator-invoke"
    far = {"box_id": "far-ok", "transport": {"kind": "ssh", "host": "far.example", "invoke": invoke_path}}
    monkeypatch.setattr(box_registry, "lock_dir", lambda home=None: tmp_path / "locks")
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [dict(far)])
    box_transport._reset_limits()
    reply = {"box_id": "far-ok", "state": "ok", "sampled_at": 1.0, "flagged": 1, "unavailable": {},
             "jobs": [{"pid": 9, "flags": ["unbooked"]}]}
    calls: list = []

    def runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), "")

    try:
        res = _REAL_INVOKE(far, "groupem.job_ledger_local", {}, runner, timeout=5)
        denied = _REAL_INVOKE(far, "groupem.capacity_admit", {"job_type": "x"}, runner, timeout=5)
    finally:
        box_transport._reset_limits()
    assert res["ok"] is True and res["reply"]["jobs"] == [{"pid": 9, "flags": ["unbooked"]}]
    assert "groupem.job_ledger_local" in calls[0][-1]
    assert denied["reason"] == "op_not_allowed" and len(calls) == 1


def _by_pid(box: dict) -> dict:
    return {j["pid"]: j for j in box["jobs"]}


@pytest.mark.parametrize("name", ("job_ledger", "job_ledger_local", "job_progress"))
def test_registered_in_registry_and_map(name: str) -> None:
    key = f"groupem.{name}"
    assert ipc._REGISTRY.get(key) is not None
    assert key in _registry_map.OP_MODULE_MAP


def test_unbooked_heavy_and_over_window_are_flagged_and_in_window_in_rate_is_clean(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(
        home,
        monkeypatch,
        [_proc(100, cmd="rmdir-storm"), _proc(200, cmd="over"), _proc(300, cmd="fine"), _proc(400, rss_mb=10.0, cmd="idle")],
        [
            _grant("go-over", 200, granted_ago=1000, window_s=60),
            _grant("go-fine", 300, granted_ago=10, window_s=600, expected_rate=1.0, progress=50),
        ],
    )
    box = _call("job_ledger", {"local_only": True})["boxes"][0]
    jobs = _by_pid(box)
    assert jobs[100]["flags"] == ["unbooked"] and jobs[100]["go_id"] is None
    assert jobs[200]["flags"] == ["over_window"] and jobs[200]["go_id"] == "go-over"
    assert jobs[200]["owner_sid"] == "sid-go-over" and jobs[200]["window_s"] == 60
    assert jobs[300]["flags"] == [] and jobs[300]["go_id"] == "go-fine"
    assert 400 not in jobs
    assert box["flagged"] == 2


def test_a_booked_job_behind_its_rate_is_flagged(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(home, monkeypatch, [_proc(500)], [_grant("go-slow", 500, 100, 600, expected_rate=10.0, progress=1)])
    box = _call("job_ledger_local", {})
    assert box["jobs"][0]["flags"] == ["behind_rate"]


def test_a_started_grant_past_its_ttl_still_lists_its_job(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(home, monkeypatch, [_proc(600)], [_grant("go-old", 600, 10_000, 100_000, ttl_s=1)])
    assert _call("job_ledger_local", {})["jobs"][0]["go_id"] == "go-old"


def test_local_only_makes_no_remote_call(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(home, monkeypatch, [], [])
    calls: list = []
    monkeypatch.setattr(box_transport, "invoke_remote", lambda *a, **k: calls.append(a) or {"ok": False})
    monkeypatch.setattr(fleet_live, "known_boxes", lambda home=None: [{"box_id": "far-1"}])
    out = _call("job_ledger", {"local_only": True})
    assert calls == [] and [b["box_id"] for b in out["boxes"]] == [out["local_box_id"]]


def test_fleet_fanout_keeps_every_far_box_with_a_reason(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(home, monkeypatch, [_proc(700)], [])
    answered = {"box_id": "far-ok", "state": "ok", "sampled_at": 1.0, "flagged": 1, "unavailable": {},
                "jobs": [{"pid": 9, "flags": ["unbooked"]}]}

    def invoke(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
        assert op_name == "groupem.job_ledger_local"
        if box["box_id"] == "far-ok":
            return {"ok": True, "reply": answered}
        if box["box_id"] == "far-hung":
            time.sleep(3)
            return {"ok": True, "reply": answered}
        if box["box_id"] == "far-old":
            return {"ok": False, "reason": "ssh_exit_1", "stderr": "method not found"}
        return {"ok": False, "reason": "ssh_exit_255"}

    monkeypatch.setattr(box_transport, "invoke_remote", invoke)
    monkeypatch.setattr(
        fleet_live, "known_boxes",
        lambda home=None: [{"box_id": b} for b in ("far-ok", "far-hung", "far-old", "far-down")],
    )
    t0 = time.monotonic()
    out = _call("job_ledger", {"timeout": 0.3})
    assert time.monotonic() - t0 < 2.0
    rows = {b["box_id"]: b for b in out["boxes"]}
    assert set(rows) == {out["local_box_id"], "far-ok", "far-hung", "far-old", "far-down"}
    assert rows["far-ok"]["state"] == "ok" and rows["far-ok"]["jobs"][0]["flags"] == ["unbooked"]
    assert [rows[b]["reason"] for b in ("far-hung", "far-old", "far-down")] == [
        "timed_out", "op_missing", "unreachable"]
    assert rows["far-hung"]["jobs"] == [] and out["flagged"] == 2 and out["jobs"] == 2


def test_far_box_gate_admits_the_ledger_ops_and_still_refuses_a_mutating_sibling() -> None:
    from coordinator_core.authz.classification import OpClass, classify
    from coordinator_core.remote_engine import policy

    for name in ("groupem.job_ledger", "groupem.job_ledger_local"):
        assert classify(name) is OpClass.COMPUTE_ONLY
        assert policy.remote_safe(name) is True
        assert box_transport._op_allowed(name) is True
    # job_progress writes a counter on a grant row, so it is MUTATING and a far box refuses it.
    assert classify("groupem.job_progress") is OpClass.MUTATING
    for name in ("groupem.job_progress", "groupem.capacity_admit"):
        assert box_transport._op_allowed(name) is False


def test_far_leg_returns_rows_when_the_remote_safe_gate_decides(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(home, monkeypatch, [], [])
    answered = {"box_id": "far-ok", "state": "ok", "sampled_at": 1.0, "flagged": 1, "unavailable": {},
                "jobs": [{"pid": 9, "flags": ["unbooked"]}]}

    def gated(box: dict, op_name: str, params: dict, runner=None, *, timeout: float) -> dict:
        if not box_transport._op_allowed(op_name):
            return {"ok": False, "state": "unknown", "reason": "op_not_allowed"}
        return {"ok": True, "reply": answered}

    monkeypatch.setattr(box_transport, "invoke_remote", gated)
    monkeypatch.setattr(fleet_live, "known_boxes", lambda home=None: [{"box_id": "far-ok"}])
    rows = {b["box_id"]: b for b in _call("job_ledger", {})["boxes"]}
    assert rows["far-ok"]["state"] == "ok" and rows["far-ok"]["jobs"] == answered["jobs"]


@pytest.mark.parametrize("params", [{"local_only": "yes"}, {"timeout": 0}, {"timeout": -1}, {"timeout": "5"}, {"timeout": True}])
def test_job_ledger_bad_params_return_error(home: Path, params: dict) -> None:
    assert "error" in op._groupem_job_ledger(params)


def test_machine_local_heavy_thresholds_change_what_the_ledger_lists(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core import machine_resolver

    _seed(home, monkeypatch, [_proc(100, rss_mb=4096.0, cmd="mid")], [])
    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: {})
    assert 100 in _by_pid(_call("job_ledger", {"local_only": True})["boxes"][0])
    monkeypatch.setattr(
        machine_resolver, "merged_flat_registry", lambda: {"group_em.job_ledger.heavy_rss_mb": "999999"}
    )
    assert 100 not in _by_pid(_call("job_ledger", {"local_only": True})["boxes"][0])
