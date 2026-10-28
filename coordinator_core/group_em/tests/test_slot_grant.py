from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import nomination, session_registry, slot_grant

NOW = 1_800_000_000.0
HOLDER, PEER = "sid-holder", "sid-peer"
RUN = "run-1"


def _row(sid, name, cwd):
    return session_registry.RegistryRow(session_id=sid, name=name, pid=1, cwd=cwd, status="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = tmp_path / "group-em"
    records.mkdir()
    live = {HOLDER, PEER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(slot_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [_row(HOLDER, "holder-name", str(repo)), _row(PEER, "peer-name", str(repo))],
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    nomination.claim(str(repo), HOLDER, directory=records, prompt_id="p-1")
    return {"dir": records, "live": live, "mp": monkeypatch}


def _issue(env, grantee=PEER, run=RUN, peak=4096, **kw):
    return slot_grant.issue_slot_grant(grantee, run, peak, "slot ruled", directory=env["dir"], now=NOW, **kw)


def _check(env, sid=PEER, run=RUN, now=NOW + 60):
    return slot_grant.check_slot_grant(sid, run, env["dir"], now)


def _files(env):
    return list((env["dir"] / "slot-grants").rglob("*.json"))


def test_issue_then_check_granted(env):
    result = _issue(env)
    assert result.ok and result.record["kind"] == slot_grant.KIND
    assert result.record["run"] == RUN and result.record["declared_peak_mb"] == 4096.0
    granted, record = _check(env)
    assert granted and record["grantee_session_id"] == PEER


def test_no_record_fails_closed(env):
    assert _check(env) == (False, None)


def test_grant_smaller_than_requested_peak_not_granted(env):
    _issue(env, peak=4096)
    assert slot_grant.check_slot_grant(PEER, RUN, env["dir"], NOW + 60, peak_mb=4096)[0] is True
    assert slot_grant.check_slot_grant(PEER, RUN, env["dir"], NOW + 60, peak_mb=8192)[0] is False


def test_other_run_not_granted(env):
    _issue(env)
    assert _check(env, run="run-2")[0] is False


def test_non_holder_issue_refused_writes_nothing(env):
    env["mp"].setenv("COORDINATOR_SESSION_ID", PEER)
    result = _issue(env)
    assert (result.ok, result.reason) == (False, slot_grant.NOT_GROUP_EM)
    assert _files(env) == []


@pytest.mark.parametrize("peak", [-1, True, "big", float("nan"), 10**12])
def test_invalid_peak_refused(env, peak):
    result = _issue(env, peak=peak)
    assert (result.ok, result.reason) == (False, slot_grant.PEAK_INVALID)


def test_bad_run_refused(env):
    assert _issue(env, run="a/b").reason == slot_grant.RUN_INVALID


def test_expired_not_granted_but_record_returned(env):
    _issue(env, ttl_minutes=1)
    granted, record = _check(env, now=NOW + 61)
    assert granted is False and record is not None


def test_ttl_above_ceiling_refused_at_issue(env):
    assert _issue(env, ttl_minutes=slot_grant.TTL_CEILING_MINUTES + 1).reason == slot_grant.TTL_OUT_OF_RANGE


def test_hand_edited_ttl_beyond_ceiling_ungranted(env):
    _issue(env)
    path = _files(env)[0]
    rec = json.loads(path.read_text())
    rec["expires_at"] = slot_grant._iso(NOW + 10 * 3600)
    path.write_text(json.dumps(rec))
    assert _check(env)[0] is False


def test_malformed_record_ungranted_never_raises(env):
    _issue(env)
    _files(env)[0].write_text("{not json")
    assert _check(env) == (False, None)


def test_grantee_not_live_ungranted(env):
    _issue(env)
    env["live"].discard(PEER)
    assert _check(env)[0] is False


def test_displaced_holder_grant_stops_authorizing(env):
    _issue(env)
    env["live"].discard(HOLDER)
    assert _check(env)[0] is False


def test_revoke_removes_and_is_idempotent(env):
    _issue(env)
    assert slot_grant.revoke_slot_grant(PEER, RUN, directory=env["dir"]).reason == "revoked"
    assert _check(env) == (False, None)
    assert slot_grant.revoke_slot_grant(PEER, RUN, directory=env["dir"]).reason == "absent"


def test_unsafe_session_id_ungranted(env):
    assert _check(env, sid="../x") == (False, None)
