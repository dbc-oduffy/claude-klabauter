"""A default groupem.roll_call through warm dispatch: light lane, far on every box, grok binds, EMs only.

One real ``_ServerContext._serve_queued`` admission path with pool size 1 and its only heavy worker
held; the op body is the real ``groupem.roll_call`` handler. Four registry boxes (issuer plus three
far, one down), a local grok EM, a local agy worker leg and subagent, and a worker leg and subagent
on a far box. ``roll_call_deliver.deliver`` then runs synchronously with the real roster resolve.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import ipc
from coordinator_core.agent_pipe import roster
from coordinator_core.group_em import box_registry, box_send, gem_home, remote_roster, roll_call_deliver
from coordinator_core.ops import group_em_roll_call  # noqa: F401 -- registers the roll-call ops
from coordinator_core.session import external_registry, peer_roster
from coordinator_core.warm import pool_sizing, server
from coordinator_core.warm.tests import test_hook_lane_full_pool as rig
from coordinator_core.warm.tests.test_hook_lane_full_pool import headroom  # noqa: F401

ISSUER = "machine-a"
FARS = ("macmini", "gipsy-linux", "machine-b")
DOWN = "machine-b"
CALLER = "sid-caller"
GROK_SID, GROK_NAME = "grok-sid-1", "grok-em"
CLAUDE_SID, AGY_LEG_SID, SUBAGENT_SID = "sid-claude", "agy:leg-1", "sid-sub"
HEAVY = "dispatch.land_run"
ROLL_CALL = "groupem.roll_call"


def _far_sid(box: str) -> str:
    return f"{box}-em"


def _peer(session_id: str, name: str, kind: str, role: str = "em", is_self: bool = False):
    return peer_roster.PeerRow(
        session_id=session_id, address=None, name=name, ref=None, cwd=None, status=None,
        running_seconds=1.0, is_self=is_self, self_determination="test", messaging_available=True,
        kind=kind, role=role,
    )


class _ParamsIO(rig._FakeIO):
    def __init__(self, method: str, request_id: int, params: dict) -> None:
        super().__init__(method, request_id)
        frame = json.loads(self._lines[0])
        frame["params"] = params
        self._lines = [(json.dumps(frame) + "\n").encode("utf-8")]


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    homes = {name: tmp_path / name for name in (ISSUER, *FARS)}
    for name, home in homes.items():
        box_registry._write(
            box_registry._identity_path(home),
            {"schema_version": box_registry.SCHEMA_VERSION, "box_id": name, "minted_at": 0},
        )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(homes[ISSUER]))
    monkeypatch.setattr(gem_home, "resolve_home", lambda: homes[ISSUER] / "gem")

    issuer_dir = box_registry._group_em_dir(homes[ISSUER])
    now = time.time()
    for name in FARS:
        box_registry.register(homes[name], {}, directory=issuer_dir, box_id=name)
        sessions = [{"session_id": _far_sid(name), "name": f"{name}-em", "role": "em"}]
        if name == "macmini":
            sessions += [
                {"session_id": "macmini-leg", "name": "macmini-leg", "role": "worker_leg"},
                {"session_id": "macmini-sub", "name": "macmini-sub", "role": "subagent"},
            ]
        remote_roster._write_snapshot(issuer_dir, name, name != DOWN, sessions, now)

    monkeypatch.setattr(
        peer_roster,
        "build_roster",
        lambda *a, **k: [
            _peer(CALLER, "caller-em", "claude", is_self=True),
            _peer(CLAUDE_SID, "claude-em", "claude"),
            _peer(GROK_SID, GROK_NAME, "grok"),
            _peer(AGY_LEG_SID, "agy-leg", "agy", role="worker_leg"),
            _peer(SUBAGENT_SID, "sub-1", "claude", role="subagent"),
        ],
    )
    grok_row = {"session_id": GROK_SID, "name": GROK_NAME, "provider": "grok"}
    monkeypatch.setattr(external_registry, "lookup", lambda sid, directory=None: None)
    monkeypatch.setattr(external_registry, "snapshot", lambda directory=None: {GROK_SID: grok_row})

    launched: list = []
    monkeypatch.setattr(
        roll_call_deliver, "launch", lambda path, **k: launched.append(Path(path)) or {"launched": True, "pid": 1}
    )
    return SimpleNamespace(homes=homes, launched=launched)


def _real_roll_call_body(release: threading.Event):
    def body(msg, caller):
        if msg["method"] == HEAVY:
            release.wait(rig._HOLD_SECS)
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": HEAVY}}
        return asyncio.run(ipc.dispatch_message({k: v for k, v in msg.items() if k != "_engine_token"}))

    return body


def _roll_call_through_warm_dispatch(headroom, fleet, tmp_path, monkeypatch) -> dict:
    release = threading.Event()
    headroom(rig._headroom(pool_sizing.RAM_RESERVE_MB // 2))
    engine_root = tmp_path / "ctx"
    engine_root.mkdir()
    ctx, heavy = rig._ctx(engine_root, monkeypatch, release, hook_io_blocked=False)
    assert heavy == 1 and server._dispatch_pool_size() == 1
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _real_roll_call_body(release))
    holder = rig._FakeIO(HEAVY, 1)
    threads = [rig._serve_async(ctx, holder)]
    try:
        assert rig._wait(lambda: ctx._admission_gate()._heavy_held >= 1, 5.0), "heavy slot never held"
        io = _ParamsIO(ROLL_CALL, 9001, {"caller_session_id": CALLER})
        started = time.monotonic()
        ctx._serve_queued(io, (time.time(), 0), lane="guard")
        elapsed = time.monotonic() - started
        assert not holder.written, "the held heavy worker must still be held"
        reply = json.loads(io.written[0])
    finally:
        rig._shutdown(ctx, release, threads)
    assert elapsed < server._HOOK_LANE_BUDGET_SECS
    assert "error" not in reply, reply
    return reply["result"]


def test_default_roll_call_runs_while_the_single_heavy_worker_is_held_and_addresses_ems_only(
    headroom, fleet, tmp_path, monkeypatch
) -> None:
    out = _roll_call_through_warm_dispatch(headroom, fleet, tmp_path, monkeypatch)

    assert {row["session_id"] for row in out["local"]} == {CLAUDE_SID, GROK_SID}
    assert {row["key"] for row in out["far"]} == {f"{box}:{_far_sid(box)}" for box in FARS}
    assert {row["box_id"] for row in out["far"]} == set(FARS)
    down_rows = [row for row in out["far"] if row["box_id"] == DOWN]
    assert down_rows and all(row.get("unreachable") for row in down_rows)
    assert {(row["key"], row["reason"]) for row in out["excluded"]} == {
        (CALLER, "self"),
        (AGY_LEG_SID, "worker_leg"),
        (SUBAGENT_SID, "subagent"),
        ("macmini:macmini-leg", "worker_leg"),
        ("macmini:macmini-sub", "subagent"),
    }
    assert fleet.launched == [Path(out["record_path"])]


def test_deliver_binds_the_grok_session_and_reaches_the_far_boxes(headroom, fleet, tmp_path, monkeypatch) -> None:
    out = _roll_call_through_warm_dispatch(headroom, fleet, tmp_path, monkeypatch)
    routed: list = []

    def route(message):
        routed.append(message)
        return SimpleNamespace(status="delivered", reason=None, wait=lambda t: None)

    appended: list = []
    assert roster.resolve(GROK_NAME) == {"kind": "grok", "id": GROK_SID, "token": None, "argv": None}
    result = roll_call_deliver.deliver(Path(out["record_path"]), append=appended.append, route=route)
    assert result["ok"] is True

    rows = group_em_roll_call_status(out["roll_call_id"])["deliveries"]
    assert rows[GROK_SID]["ok"] is True and rows[GROK_SID]["reason"] != "no_binding"
    assert [m.to_identity for m in routed] == [GROK_NAME]
    assert [m.to_session for m in appended] == [CLAUDE_SID]
    for box in FARS:
        row = rows[f"{box}:{_far_sid(box)}"]
        if box == DOWN:
            assert row["ok"] is False and row["reason"] == "box_unreachable"
        else:
            assert row["ok"] is True
            assert [line["text"] for line in box_send.read_inbox(_far_sid(box), home=fleet.homes[box])] == [out["text"]]
    assert set(rows) == {CLAUDE_SID, GROK_SID} | {f"{box}:{_far_sid(box)}" for box in FARS}


def test_an_unbound_non_claude_name_still_reports_no_binding(headroom, fleet, tmp_path, monkeypatch) -> None:
    out = _roll_call_through_warm_dispatch(headroom, fleet, tmp_path, monkeypatch)
    monkeypatch.setattr(external_registry, "snapshot", lambda directory=None: {})
    roll_call_deliver.deliver(
        Path(out["record_path"]),
        append=lambda m: None,
        route=lambda m: SimpleNamespace(status="delivered", reason=None, wait=lambda t: None),
    )
    rows = group_em_roll_call_status(out["roll_call_id"])["deliveries"]
    assert rows[GROK_SID] == {"ok": False, "reason": "no_binding", "at": rows[GROK_SID]["at"]}


def group_em_roll_call_status(roll_call_id: str) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": "groupem.roll_call_status", "params": {"roll_call_id": roll_call_id}}
    resp = asyncio.run(ipc.dispatch_message(msg))
    assert "error" not in resp, resp
    return resp["result"]
