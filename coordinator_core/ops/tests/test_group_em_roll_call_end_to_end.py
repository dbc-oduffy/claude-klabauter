"""End to end: one default-argument groupem.roll_call reaches every live session, and a far reply lands.

Two boxes share one process, each a settings home. The issuing box lists a local Claude and a local
Codex session, a far box reached through FsTransport, and a down far box. The far reply travels
through the real ``box_transport.invoke_remote`` argv with only the ssh spawn replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import (
    box_registry,
    box_send,
    box_transport,
    gem_home,
    remote_roster,
    roll_call_deliver,
)
from coordinator_core.ops import group_em_roll_call  # noqa: F401 -- registers the three ops
from coordinator_core.session import peer_roster

ISSUER, FAR, DOWN = "boxa", "boxb", "boxc"
CALLER = "sid-caller"
CLAUDE_SID, CODEX_SID, MIRROR_SID = "sid-claude", "codex:cx1", "peer-codex-dup"
FAR_SID, DOWN_SID = "sess-far", "sess-down"
FAR_KEY, DOWN_KEY = f"{FAR}:{FAR_SID}", f"{DOWN}:{DOWN_SID}"
ADDRESSEE_KEYS = {CLAUDE_SID, CODEX_SID, FAR_KEY, DOWN_KEY}


def _peer(session_id: str, name: str, kind: str, is_self: bool = False) -> peer_roster.PeerRow:
    return peer_roster.PeerRow(
        session_id=session_id,
        address=None,
        name=name,
        ref=None,
        cwd=None,
        status=None,
        running_seconds=1.0,
        is_self=is_self,
        self_determination="test",
        messaging_available=True,
        kind=kind,
    )


def _goal() -> dict:
    return {
        "goal_id": "G1", "target": "p95 < 500ms", "measured_now": "420ms", "gap": "none",
        "next_lever": "cache roster", "expected_gain": "-80ms", "owner": "far-em", "eta": "2026-12-01",
    }


def _dispatch(method: str, params: dict) -> dict:
    """The op's JSON-RPC result through ipc dispatch; a fresh thread keeps nested dispatch legal."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with ThreadPoolExecutor(max_workers=1) as pool:
        resp = pool.submit(lambda: asyncio.run(ipc.dispatch_message(msg))).result()
    assert "error" not in resp, resp
    return resp["result"]


class _Boxes:
    def __init__(self, root: Path) -> None:
        self.homes = {ISSUER: root / ISSUER, FAR: root / FAR}
        self.current = ISSUER

    def gem(self) -> Path:
        return self.homes[self.current] / "gem"

    @contextlib.contextmanager
    def as_box(self, name: str):
        previous_box, previous_env = self.current, os.environ.get("COORDINATOR_SETTINGS_HOME")
        self.current = name
        os.environ["COORDINATOR_SETTINGS_HOME"] = str(self.homes[name])
        try:
            yield
        finally:
            self.current = previous_box
            if previous_env is None:
                os.environ.pop("COORDINATOR_SETTINGS_HOME", None)
            else:
                os.environ["COORDINATOR_SETTINGS_HOME"] = previous_env


@pytest.fixture
def boxes(monkeypatch, tmp_path):
    fleet = _Boxes(tmp_path)
    for name, home in fleet.homes.items():
        box_registry._write(
            box_registry._identity_path(home),
            {"schema_version": box_registry.SCHEMA_VERSION, "box_id": name, "minted_at": 0},
        )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(fleet.homes[ISSUER]))
    monkeypatch.setattr(gem_home, "resolve_home", fleet.gem)

    issuer_dir = box_registry._group_em_dir(fleet.homes[ISSUER])
    box_registry.register(fleet.homes[FAR], {}, directory=issuer_dir, box_id=FAR)
    now = time.time()
    remote_roster._write_snapshot(
        issuer_dir, FAR, True, [{"session_id": FAR_SID, "name": "far-em"}], now
    )
    remote_roster._write_snapshot(
        issuer_dir, DOWN, False, [{"session_id": DOWN_SID, "name": "down-em"}], now
    )

    far_dir = box_registry._group_em_dir(fleet.homes[FAR])
    box_registry.register(
        fleet.homes[ISSUER],
        {},
        directory=far_dir,
        box_id=ISSUER,
        transport={"kind": "ssh", "host": "192.0.2.1", "invoke": "/x/coordinator-invoke"},
    )

    monkeypatch.setattr(
        peer_roster,
        "build_roster",
        lambda *a, **k: [
            _peer(CALLER, "caller-em", "claude", is_self=True),
            _peer(CLAUDE_SID, "claude-em", "claude"),
            _peer(CODEX_SID, "codex-em", "codex"),
            _peer(MIRROR_SID, "codex-mirror", "codex"),
        ],
    )

    launched: list = []
    monkeypatch.setattr(
        roll_call_deliver, "launch", lambda path, **k: launched.append(Path(path)) or {"launched": True, "pid": 1}
    )
    fleet.launched = launched

    box_transport._reset_limits()
    real_which = box_transport.shutil.which
    monkeypatch.setattr(
        box_transport.shutil, "which", lambda name, *a, **k: "ssh-stub" if name == "ssh" else real_which(name, *a, **k)
    )
    fleet.ssh_calls = []

    def ssh_runner(argv, timeout):
        invoke, op, text = shlex.split(argv[-1])
        params = json.loads(text)
        fleet.ssh_calls.append((argv[-2], op, params))
        with fleet.as_box(ISSUER):
            result = _dispatch(op, params)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")

    monkeypatch.setattr(box_transport, "_run", ssh_runner)
    yield fleet
    box_transport._reset_limits()


def _open_roll_call(fleet: _Boxes) -> dict:
    with fleet.as_box(ISSUER):
        return _dispatch("groupem.roll_call", {"caller_session_id": CALLER})


def _deliver_on_issuer(fleet: _Boxes, record_path: str):
    appended: list = []
    routed: list = []

    def route(message):
        routed.append(message)
        return SimpleNamespace(status="delivered", reason=None, wait=lambda t: None)

    with fleet.as_box(ISSUER):
        result = roll_call_deliver.deliver(
            Path(record_path),
            append=appended.append,
            route=route,
            resolve=lambda name: {"kind": "codex", "name": name},
        )
    return result, appended, routed


def test_default_call_addresses_every_live_session_and_launches_one_worker(boxes):
    out = _open_roll_call(boxes)
    assert {row["session_id"] for row in out["local"]} == {CLAUDE_SID, CODEX_SID}
    assert {row["kind"] for row in out["local"]} == {"claude", "codex"}
    assert {row["key"] for row in out["far"]} == {FAR_KEY, DOWN_KEY}
    assert {(row["key"], row["reason"]) for row in out["excluded"]} == {
        (CALLER, "self"),
        (MIRROR_SID, "codex_mirror"),
    }
    assert out["delivery"] == {"launched": True, "pid": 1}
    assert boxes.launched == [Path(out["record_path"])]


def test_ask_names_the_issuing_box_so_a_far_reply_can_route_home(boxes):
    out = _open_roll_call(boxes)
    assert "origin_box" in out["text"] and ISSUER in out["text"]


def test_one_deliveries_row_per_addressee_key_and_each_channel_is_used(boxes):
    out = _open_roll_call(boxes)
    result, appended, routed = _deliver_on_issuer(boxes, out["record_path"])
    assert result["ok"] is True

    with boxes.as_box(ISSUER):
        status = _dispatch("groupem.roll_call_status", {"roll_call_id": out["roll_call_id"]})
    rows = status["deliveries"]
    assert set(rows) == ADDRESSEE_KEYS
    assert all(set(row) == {"ok", "reason", "at"} for row in rows.values())
    assert rows[CLAUDE_SID]["ok"] is True
    assert rows[CODEX_SID]["ok"] is True
    assert rows[FAR_KEY]["ok"] is True
    assert rows[DOWN_KEY] == {"ok": False, "reason": "box_unreachable", "at": rows[DOWN_KEY]["at"]}

    assert [m.to_session for m in appended] == [CLAUDE_SID]
    assert appended[0].content == out["text"]
    assert [(m.to_identity, m.content) for m in routed] == [("codex-em", out["text"])]
    far_inbox = box_send.read_inbox(FAR_SID, home=boxes.homes[FAR])
    assert [line["text"] for line in far_inbox] == [out["text"]]
    assert box_send.read_inbox(DOWN_SID, home=boxes.homes[FAR]) == []


def test_rerun_of_the_worker_sends_nothing_again(boxes):
    out = _open_roll_call(boxes)
    _deliver_on_issuer(boxes, out["record_path"])
    _, appended, routed = _deliver_on_issuer(boxes, out["record_path"])
    assert appended == [] and routed == []
    assert len(box_send.read_inbox(FAR_SID, home=boxes.homes[FAR])) == 1


def test_far_reply_lands_on_the_issuing_box_record_and_status_shows_it_replied(boxes):
    out = _open_roll_call(boxes)
    _deliver_on_issuer(boxes, out["record_path"])
    rc = out["roll_call_id"]

    with boxes.as_box(FAR):
        reply = _dispatch(
            "groupem.roll_call_reply",
            {
                "roll_call_id": rc,
                "session_key": FAR_KEY,
                "goals": [_goal()],
                "priority_goal_id": "G1",
                "origin_box": ISSUER,
            },
        )
        assert gem_home.resolve_home() == boxes.homes[FAR] / "gem"
        assert not (boxes.homes[FAR] / "gem" / "roll-call").exists()
    assert reply["ok"] is True and reply["result"] == "stored"
    ((host, op, params),) = boxes.ssh_calls
    assert (host, op) == ("192.0.2.1", "groupem.roll_call_reply")
    assert "origin_box" not in params and params["forwarded_by"] == FAR

    with boxes.as_box(ISSUER):
        status = _dispatch("groupem.roll_call_status", {"roll_call_id": rc})
    assert FAR_KEY in status["replied"]
    assert FAR_KEY not in {row["key"] for row in status["missing"]}
    assert [g["session_key"] for g in status["goals"]] == [FAR_KEY]


def test_far_reply_at_default_params_reaches_the_minting_store_with_origin_box(boxes):
    out = _open_roll_call(boxes)
    rc = out["roll_call_id"]

    with boxes.as_box(FAR):
        reply = _dispatch(
            "groupem.roll_call_reply",
            {"roll_call_id": rc, "session_key": FAR_KEY, "goals": [_goal()], "priority_goal_id": "G1"},
        )
        assert not (boxes.homes[FAR] / "gem" / "roll-call").exists()
    assert reply["ok"] is True and reply["result"] == "stored"
    ((host, op, params),) = boxes.ssh_calls
    assert (host, op) == ("192.0.2.1", "groupem.roll_call_reply")
    assert params["forwarded_by"] == FAR

    with boxes.as_box(ISSUER):
        stored = json.loads(Path(out["record_path"]).read_text(encoding="utf-8"))
    assert stored["replies"][FAR_KEY]["origin_box"] == ISSUER
    assert stored["replies"][FAR_KEY]["forwarded_by"] == FAR


def test_forwarded_reply_is_refused_for_a_non_far_key_and_an_unregistered_origin(boxes):
    out = _open_roll_call(boxes)
    rc = out["roll_call_id"]
    base = {"roll_call_id": rc, "goals": [_goal()], "priority_goal_id": "G1"}

    with boxes.as_box(FAR):
        local_key = _dispatch(
            "groupem.roll_call_reply", {**base, "session_key": CLAUDE_SID, "origin_box": ISSUER}
        )
        before_unknown = len(boxes.ssh_calls)
        unknown = _dispatch(
            "groupem.roll_call_reply", {**base, "session_key": FAR_KEY, "origin_box": "nowhere"}
        )
    assert local_key == {"ok": False, "reason": "not_an_addressee"}
    assert unknown == {"ok": False, "reason": "unknown_origin_box"}
    assert len(boxes.ssh_calls) == before_unknown

    with boxes.as_box(ISSUER):
        status = _dispatch("groupem.roll_call_status", {"roll_call_id": rc})
    assert status["replied"] == []


def test_the_reply_op_is_admitted_by_the_ssh_allowlist():
    assert "groupem.roll_call_reply" in box_transport.ALLOWED_OPS
