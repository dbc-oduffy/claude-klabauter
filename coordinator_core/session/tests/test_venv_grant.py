from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops import baton_pm_turns
from coordinator_core.session import core, venv_grant as vg

SID = "sid-venv"
T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    hub = tmp_path / "coordinator-sessions"
    hub.mkdir()
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: str(hub))
    (hub / SID).mkdir()
    baton_pm_turns.append_turn("zero launch", session_id=SID)
    baton_pm_turns.append_turn("yes, one isolated venv under the sandbox is fine " + "x" * 400, session_id=SID)
    prefix = tmp_path / "work" / "sandbox"
    return hub, prefix


def _grant(prefix, **kw):
    return vg.write_venv_grant(1, str(prefix), session_id=SID, now=T0, **kw)


def _check(target, now=T0, sid=SID):
    return vg.check_venv_grant(str(target), session_id=sid, now=now)[0]


def test_issue_then_check_in_prefix_live(env):
    hub, prefix = env
    res = _grant(prefix)
    assert res.ok and res.record["pm_turn"] == 1
    assert len(res.record["pm_turn_excerpt"]) == 200
    assert json.loads((hub / SID / "venv-grant.json").read_text())["kind"] == "venv-grant"
    assert _check(prefix)
    assert _check(prefix / ".venv")


def test_out_of_prefix_and_sibling_prefix_refused(env):
    _, prefix = env
    _grant(prefix)
    assert not _check(prefix.parent / "elsewhere" / ".venv")
    assert not _check(str(prefix) + "-other")


def test_expired_refused(env):
    _, prefix = env
    _grant(prefix, ttl_minutes=10)
    assert _check(prefix, now=T0 + timedelta(minutes=9))
    assert not _check(prefix, now=T0 + timedelta(minutes=10))


def test_no_grant_refused(env):
    _, prefix = env
    assert not _check(prefix)


def test_refuses_absent_turn_and_negative_turn(env):
    _, prefix = env
    assert not vg.write_venv_grant(9, str(prefix), session_id=SID).ok
    res = vg.write_venv_grant(-1, str(prefix), session_id=SID)
    assert not res.ok and res.reason == vg.BAD_TURN
    assert not vg.write_venv_grant(True, str(prefix), session_id=SID).ok


@pytest.mark.parametrize("bad", ["", "   ", "relative/dir", "."])
def test_refuses_bad_prefix(env, bad):
    res = vg.write_venv_grant(1, bad, session_id=SID)
    assert not res.ok and res.reason == vg.BAD_PREFIX


def test_refuses_filesystem_root(env, tmp_path):
    root = tmp_path.anchor
    assert not vg.write_venv_grant(1, root, session_id=SID).ok


@pytest.mark.parametrize("ttl", [0, -5, vg.TTL_CEILING_MINUTES + 1, "60"])
def test_refuses_ttl_out_of_range(env, ttl):
    _, prefix = env
    res = _grant(prefix, ttl_minutes=ttl)
    assert not res.ok and res.reason == vg.BAD_TTL


def test_ttl_ceiling_boundary_accepted(env):
    _, prefix = env
    assert _grant(prefix, ttl_minutes=vg.TTL_CEILING_MINUTES).ok


def test_ceiling_rechecked_on_read(env):
    hub, prefix = env
    _grant(prefix)
    path = hub / SID / "venv-grant.json"
    rec = json.loads(path.read_text())
    rec["expires_at"] = (T0 + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(rec))
    assert not _check(prefix)


@pytest.mark.parametrize("payload", ["not json", "[]", "{}", '{"kind": "venv-grant"}'])
def test_malformed_record_ungranted(env, payload):
    hub, prefix = env
    (hub / SID / "venv-grant.json").write_text(payload)
    assert not _check(prefix)


def test_record_for_other_session_ungranted(env):
    hub, prefix = env
    _grant(prefix)
    rec = json.loads((hub / SID / "venv-grant.json").read_text())
    rec["session_id"] = "someone-else"
    (hub / SID / "venv-grant.json").write_text(json.dumps(rec))
    assert not _check(prefix)


def test_other_session_does_not_read_this_grant(env):
    hub, prefix = env
    _grant(prefix)
    (hub / "sid-peer").mkdir()
    assert not _check(prefix, sid="sid-peer")


def test_relative_or_empty_target_refused(env):
    _, prefix = env
    _grant(prefix)
    assert not vg.check_venv_grant("", session_id=SID, now=T0)[0]
    assert not vg.check_venv_grant("sandbox/.venv", session_id=SID, now=T0)[0]


def test_check_never_raises(env, monkeypatch):
    _, prefix = env
    monkeypatch.setattr(vg, "_live_record", lambda *a: 1 / 0)
    assert vg.check_venv_grant(str(prefix), session_id=SID) == (False, None)


def test_reissue_overwrites_and_revoke_clears(env, tmp_path):
    _, prefix = env
    _grant(prefix)
    other = tmp_path / "other"
    assert _grant(other).ok
    assert not _check(prefix) and _check(other)
    assert vg.revoke_venv_grant(session_id=SID).ok
    assert not _check(other)
    assert vg.revoke_venv_grant(session_id=SID).ok
