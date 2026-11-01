"""End-to-end loopback: a session on a second box through ``watch.poll_once`` and ``resolve_addressee``.

Two settings-homes under ``tmp_path``. The primary registers the far one over an fs
transport; the far box publishes its own roster, the primary's ``remote_roster.refresh``
copies it into a snapshot, and ``poll_once`` / ``resolve_addressee`` read it at default
arguments (the settings home comes from the environment, never an injected directory).
Only the local harness registry is stubbed, as ``test_watch.py`` does.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from coordinator_core.group_em import box_registry, remote_roster, send_pass, watch

REPO_ROOT = "/repo/root"
FAR_ID = "boxfar"
SID = "far-sess-1"
KEY = f"{FAR_ID}:{SID}"
NAME = "far-peer-1"


def _far_session(sid=SID, name=NAME, **extra):
    return {"session_id": sid, "name": name, "cwd": "/far/repo", "status": "busy", "address": name, **extra}


@pytest.fixture
def boxes(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    far = tmp_path / "far"
    primary_dir = box_registry._group_em_dir(primary)
    far_dir = box_registry._group_em_dir(far)
    primary_dir.mkdir(parents=True)
    far_dir.mkdir(parents=True)
    (far_dir / "box.json").write_text(
        '{"schema_version": 1, "box_id": "%s", "minted_at": 0}' % FAR_ID, encoding="utf-8"
    )
    box_registry.register(far, {"slots": 1}, directory=primary_dir)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(primary))
    assert box_registry.local_box_id() == box_registry.LOCAL_BOX_ID
    return primary_dir, far_dir


def _publish_and_refresh(boxes, sessions):
    primary_dir, far_dir = boxes
    now = time.time()
    assert remote_roster.publish_local(far_dir, sessions, now=now, local_id=FAR_ID)
    assert remote_roster.refresh(primary_dir, now=now, interval_s=0) == {FAR_ID: True}


def _break_far_publication(boxes):
    """The far box stops publishing; the primary's next refresh records ok=false."""
    primary_dir, far_dir = boxes
    remote_roster.snapshot_path(far_dir, FAR_ID).unlink()
    assert remote_roster.refresh(primary_dir, interval_s=0) == {FAR_ID: False}


def _poll(prev_parked=None, prev_names=None):
    emitted: list[str] = []
    local = [{"sessionId": "local-1", "status": "busy", "cwd": REPO_ROOT, "name": "local-peer"}]
    with mock.patch.object(watch.read_pass, "fetch_live_agents", return_value=local), mock.patch.object(
        watch.read_pass, "enumerate_repo_peers", side_effect=lambda a, sid: list(a)
    ), mock.patch.object(watch.read_pass, "classify_peer") as classify:
        classify.side_effect = lambda root, peer, now=None: (
            watch.read_pass._classify_remote(peer, peer["sessionId"])
            if peer.get("remote")
            else {"state": "PRODUCING", "reason": "tool-use", "candidate": False}
        )
        result = watch.poll_once(
            REPO_ROOT,
            "waker-1",
            prev_parked if prev_parked is not None else {},
            now=datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc),
            emit=emitted.append,
            group_em_session_id="group-em-1",
            prev_names=prev_names,
        )
    return result, emitted


def _gone(emitted):
    return [line for line in emitted if line.startswith("GONE")]


def test_remote_session_in_roster(boxes):
    _publish_and_refresh(boxes, [_far_session()])
    (parked, _decl, notes, _inbox), emitted = _poll()
    assert set(parked) == {"local-1", KEY}
    assert notes[KEY]["name"] == NAME
    assert _gone(emitted) == []


def test_resolve_addressee_remote(boxes):
    _publish_and_refresh(
        boxes,
        [_far_session(), _far_session("far-sess-2", "dup"), _far_session("far-sess-3", "dup")],
    )
    assert send_pass.resolve_addressee(REPO_ROOT, KEY) == NAME
    assert send_pass.resolve_addressee(REPO_ROOT, f"{FAR_ID}:far-sess-2") is None
    assert send_pass.resolve_addressee(REPO_ROOT, f"{FAR_ID}:no-such-sid") is None
    assert send_pass.resolve_addressee(REPO_ROOT, f"nobox:{SID}") is None


def test_mirror_swap_no_gone(boxes):
    _publish_and_refresh(boxes, [_far_session(pid=111)])
    (parked, _d, notes, _i), _ = _poll()
    assert KEY in parked
    _publish_and_refresh(boxes, [_far_session(pid=222)])
    (parked2, _d2, _n2, _i2), emitted = _poll(prev_parked=parked, prev_names=notes)
    assert KEY in parked2
    assert _gone(emitted) == []


def test_unreachable_box_no_gone(boxes):
    _publish_and_refresh(boxes, [_far_session()])
    (parked, _d, notes, _i), _ = _poll()
    _break_far_publication(boxes)
    (parked2, _d2, notes2, _i2), emitted = _poll(prev_parked=parked, prev_names=notes)
    assert _gone(emitted) == []
    assert KEY in parked2 and notes2[KEY]["name"] == NAME


def test_true_departure_gone(boxes):
    _publish_and_refresh(boxes, [_far_session(), _far_session("far-sess-9", "stays")])
    (parked, _d, notes, _i), _ = _poll()
    assert f"{FAR_ID}:far-sess-9" in parked
    _publish_and_refresh(boxes, [_far_session("far-sess-9", "stays")])
    (parked2, _d2, _n2, _i2), emitted = _poll(prev_parked=parked, prev_names=notes)
    gone = _gone(emitted)
    assert len(gone) == 1 and f"[{KEY}]" in gone[0]
    assert KEY not in parked2
