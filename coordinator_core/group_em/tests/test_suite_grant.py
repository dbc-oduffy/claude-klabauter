from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.group_em import nomination, session_registry, suite_grant
from coordinator_core.session import grant as session_grant

NOW = 1_800_000_000.0
HOLDER, PEER, OTHER = "sid-holder", "sid-peer", "sid-other"


def _row(sid, name, cwd):
    return session_registry.RegistryRow(session_id=sid, name=name, pid=1, cwd=cwd, status="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = tmp_path / "group-em"
    records.mkdir()
    live = {HOLDER, PEER, OTHER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            _row(HOLDER, "holder-name", str(repo)),
            _row(PEER, "peer-name", str(repo)),
            _row(OTHER, "other-name", str(repo)),
        ],
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    nomination.claim(str(repo), HOLDER, directory=records, prompt_id="p-1")
    return {"repo": str(repo), "dir": records, "live": live, "mp": monkeypatch}


def _issue(env, grantee=PEER, **kw):
    return suite_grant.issue_suite_grant(grantee, "ruled: one broad run", directory=env["dir"], now=NOW, **kw)


def _check(env, sid=PEER, root=None, now=NOW + 60):
    return suite_grant.check_group_em_suite_grant(
        env["repo"], session_id=sid, repo_root=root or env["repo"], directory=env["dir"], now=now
    )


def _grant_files(env):
    return list((env["dir"] / "suite-grants").rglob("*.json"))


def test_holder_issues_and_grantee_checks_granted(env):
    result = _issue(env)
    assert result.ok and result.record["issuer_session_id"] == HOLDER
    assert result.record["grantee_session_id"] == PEER
    assert result.record["note"] == "ruled: one broad run"
    granted, record = _check(env)
    assert granted and record["grantee_session_id"] == PEER


def test_grantee_resolves_by_registry_name(env):
    assert _issue(env, "peer-name").record["grantee_session_id"] == PEER


def test_holder_may_grant_itself(env):
    _issue(env, HOLDER)
    assert _check(env, HOLDER)[0]


def test_non_holder_issue_refused_and_writes_nothing(env):
    env["mp"].setenv("COORDINATOR_SESSION_ID", PEER)
    result = _issue(env)
    assert (result.ok, result.reason) == (False, suite_grant.NOT_GROUP_EM)
    assert _grant_files(env) == []


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_unverified_holder_is_refused(env, status):
    env["mp"].setattr(nomination, "entry_status", lambda *a, **k: {"status": status})
    assert _issue(env).reason == suite_grant.NOT_GROUP_EM
    assert _grant_files(env) == []


def test_dead_holder_is_refused(env):
    env["live"].discard(HOLDER)
    assert _issue(env).reason == suite_grant.GROUP_EM_NOT_LIVE


def test_unresolved_issuer_is_refused(env):
    env["mp"].setattr(suite_grant.core, "resolve_session_id", lambda cwd=None: "")
    assert _issue(env).reason == suite_grant.ISSUER_UNRESOLVED


def test_empty_note_ttl_and_grantee_refusals(env):
    assert suite_grant.issue_suite_grant(PEER, "  ", directory=env["dir"], now=NOW).reason == suite_grant.NOTE_EMPTY
    assert _issue(env, ttl_minutes=0).reason == suite_grant.TTL_OUT_OF_RANGE
    assert _issue(env, ttl_minutes=721).reason == suite_grant.TTL_OUT_OF_RANGE
    assert _issue(env, "nobody").reason == suite_grant.GRANTEE_UNRESOLVED
    env["live"].discard(PEER)
    assert _issue(env).reason == suite_grant.GRANTEE_NOT_LIVE
    assert _grant_files(env) == []


def test_ttl_ceiling_is_accepted(env):
    assert _issue(env, ttl_minutes=720).ok and _check(env)[0]


def test_file_planted_under_a_non_holder_key_is_never_read(env):
    _issue(env)
    holder_file = env["dir"] / "suite-grants" / HOLDER / f"{PEER}.json"
    planted = env["dir"] / "suite-grants" / OTHER / f"{PEER}.json"
    planted.parent.mkdir(parents=True)
    planted.write_text(holder_file.read_text(encoding="utf-8"), encoding="utf-8")
    holder_file.unlink()
    assert _check(env) == (False, None)


def test_another_session_reads_ungranted(env):
    _issue(env)
    assert _check(env, OTHER) == (False, None)


def test_expired_grant_reads_ungranted_with_its_record(env):
    _issue(env, ttl_minutes=10)
    granted, record = _check(env, now=NOW + 601)
    assert not granted and record["grantee_session_id"] == PEER
    assert not _check(env, now=NOW - 1)[0]


def test_record_over_the_ceiling_reads_ungranted(env):
    _issue(env)
    path = _grant_files(env)[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expires_at"] = suite_grant._iso(NOW + 721 * 60)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not _check(env, now=NOW + 60)[0]


def test_displaced_holder_grant_reads_ungranted(env):
    _issue(env)
    nomination.claim(env["repo"], OTHER, directory=env["dir"], prompt_id="p-2")
    assert not _check(env)[0]


def test_standing_down_holder_grant_reads_ungranted(env):
    _issue(env)
    env["live"].discard(HOLDER)
    assert not _check(env)[0]


def test_unverified_holder_grant_reads_ungranted(env):
    _issue(env)
    env["mp"].setattr(nomination, "entry_status", lambda *a, **k: {"status": "pending"})
    assert not _check(env)[0]


def test_dead_grantee_reads_ungranted(env):
    _issue(env)
    env["live"].discard(PEER)
    assert not _check(env)[0]


def test_scope_root_is_component_wise(env, tmp_path):
    _issue(env, repo=str(tmp_path / "r" / "a"))
    assert _check(env, root=str(tmp_path / "r" / "a"))[0]
    assert _check(env, root=str(tmp_path / "r" / "a" / "sub"))[0]
    assert not _check(env, root=str(tmp_path / "r" / "ab"))[0]
    assert not _check(env, root=str(tmp_path / "r"))[0]
    assert not _check(env, root=env["repo"])[0]


def test_malformed_json_reads_ungranted(env):
    _issue(env)
    _grant_files(env)[0].write_text("{not json", encoding="utf-8")
    assert _check(env) == (False, None)
    _grant_files(env)[0].write_text("[1]", encoding="utf-8")
    assert _check(env) == (False, None)


def test_unsafe_session_id_reads_ungranted(env):
    assert _check(env, "../x") == (False, None)
    assert _check(env, "") == (False, None)


def test_reissue_overwrites_and_revoke_is_idempotent(env):
    _issue(env, ttl_minutes=5)
    _issue(env, ttl_minutes=60)
    assert len(_grant_files(env)) == 1 and _check(env, now=NOW + 3000)[0]
    first = suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    again = suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    assert (first.ok, first.reason, again.ok, again.reason) == (True, "revoked", True, "absent")
    assert not _check(env)[0]


def test_revoke_touches_only_the_callers_key(env):
    _issue(env)
    env["mp"].setenv("COORDINATOR_SESSION_ID", OTHER)
    suite_grant.revoke_suite_grant(PEER, directory=env["dir"])
    assert _check(env)[0]


def test_check_spawns_nothing(env, monkeypatch):
    _issue(env)

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert _check(env)[0]


def test_check_never_raises(env):
    env["mp"].setattr(nomination, "box_holder", lambda *a, **k: 1 / 0)
    assert _check(env) == (False, None)


def test_human_grant_enum_is_unchanged():
    assert session_grant._VALID_GRANTED_BY == frozenset({"pm", "ceremony"})
