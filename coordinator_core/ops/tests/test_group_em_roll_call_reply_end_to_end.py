"""End to end: an EM's groupem.roll_call_reply records against a live roll call through ipc dispatch.

Pins the three refusals that kept every EM from replying: session_key required, the per-goal
payload, and a forward with no invoke path.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.group_em import box_registry, box_transport, gem_home, remote_roster, roll_call_deliver
from coordinator_core.ops import group_em_roll_call  # noqa: F401 -- registers the three ops
from coordinator_core.session import peer_roster

ISSUER, FAR = "boxa", "boxb"
EM_SID = "sid-em"
FAR_SID = "sess-far"
FAR_KEY = f"{FAR}:{FAR_SID}"


def _peer(session_id: str, name: str, is_self: bool = False) -> peer_roster.PeerRow:
    return peer_roster.PeerRow(
        session_id=session_id, address=None, name=name, ref=None, cwd=None, status=None,
        running_seconds=1.0, is_self=is_self, self_determination="test", messaging_available=True,
        kind="claude",
    )


def _dispatch(method: str, params: dict) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with ThreadPoolExecutor(max_workers=1) as pool:
        resp = pool.submit(lambda: asyncio.run(ipc.dispatch_message(msg))).result()
    assert "error" not in resp, resp
    return resp["result"]


@pytest.fixture
def live(monkeypatch, tmp_path):
    home = tmp_path / ISSUER
    box_registry._write(
        box_registry._identity_path(home),
        {"schema_version": box_registry.SCHEMA_VERSION, "box_id": ISSUER, "minted_at": 0},
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home / "gem")
    issuer_dir = box_registry._group_em_dir(home)
    box_registry.register(
        tmp_path / FAR, {}, directory=issuer_dir, box_id=FAR,
        transport={"kind": "ssh", "host": "192.0.2.1"},
    )
    remote_roster._write_snapshot(issuer_dir, FAR, True, [{"session_id": FAR_SID, "name": "far-em"}], time.time())
    monkeypatch.setattr(
        peer_roster, "build_roster",
        lambda *a, **k: [_peer("sid-caller", "caller", True), _peer(EM_SID, "em-one")],
    )
    monkeypatch.setattr(roll_call_deliver, "launch", lambda path, **k: {"launched": True, "pid": 1})
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", EM_SID)
    return home


def _open() -> str:
    return _dispatch("groupem.roll_call", {"caller_session_id": "sid-caller"})["roll_call_id"]


def test_em_replies_with_no_session_key_and_no_goals_and_status_shows_it(live):
    rc = _open()
    jobs = [{"id": "run-1", "pid": 4242, "cores": 4, "hard_stop": "2026-11-28T06:00:00Z"}]
    out = _dispatch("groupem.roll_call_reply", {"roll_call_id": rc, "jobs": jobs, "queued": ["plan-a"], "eta": "2026-11-28"})
    assert out["ok"] is True and out["result"] == "stored"

    status = _dispatch("groupem.roll_call_status", {"roll_call_id": rc})
    assert EM_SID in status["replied"]
    assert EM_SID not in {row["key"] for row in status["missing"]}
    assert status["reports"] == [
        {"session_key": EM_SID, "late": False, "jobs": jobs, "queued": ["plan-a"], "eta": "2026-11-28"}
    ]


def test_goals_reply_still_records_and_a_bad_goal_is_still_refused(live):
    rc = _open()
    goal = {
        "goal_id": "G1", "target": "t", "measured_now": "m", "gap": "g", "next_lever": "n",
        "expected_gain": "e", "owner": "o", "eta": "2026-12-01",
    }
    ok = _dispatch("groupem.roll_call_reply", {"roll_call_id": rc, "goals": [goal], "priority_goal_id": "G1"})
    assert ok["result"] == "stored"
    refused = _dispatch(
        "groupem.roll_call_reply",
        {"roll_call_id": rc, "session_key": "other", "goals": [{**goal, "gap": ""}], "priority_goal_id": "G1"},
    )
    assert refused["error"] == "RollCallReplyRefused"


def test_reply_naming_an_origin_box_with_no_invoke_path_is_stored_locally(live, monkeypatch):
    real = box_transport.shutil.which
    monkeypatch.setattr(
        box_transport.shutil, "which", lambda name, *a, **k: "ssh-stub" if name == "ssh" else real(name, *a, **k)
    )
    out = _dispatch(
        "groupem.roll_call_reply",
        {"roll_call_id": "rc-minted-elsewhere", "session_key": FAR_KEY, "eta": "2026-11-28", "origin_box": FAR},
    )
    assert out["ok"] is True and out["result"] == "stored" and out["stored_locally"] is True
    assert out["forward_reason"] == "forward_failed:no_invoke_path"
    stored = json.loads(
        (Path(gem_home.resolve_home()) / "roll-call" / "rc-minted-elsewhere.json").read_text(encoding="utf-8")
    )
    assert FAR_KEY in stored["replies"]
    assert stored["replies"][FAR_KEY]["origin_box"] == FAR
