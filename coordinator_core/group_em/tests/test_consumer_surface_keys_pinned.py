"""Pins the key set of each Group EM record a cockpit consumer reads, against its writer.

The contract is coordinator_core/contract/group-em-records-producer-contract.md. The watch
heartbeat (`state/group-em-watch.json`) is pinned elsewhere, in test_watch_heartbeat.py.
Every test writes under tmp_path; the real settings home is never touched.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import box_hold, nomination, session_registry, standing, suite_grant

HOLDER, PEER, OTHER = "sid-holder", "sid-peer", "sid-other"
NOW = 1_800_000_000.0

NOMINATION_KEYS = {
    "version", "repo_root", "session_id", "peer_name", "nominated_at", "nominated_by", "note",
    "entered_via", "entry_evidence",
}
DISPLACED_EXTRA_KEYS = {"displaced_holder", "displaced_holder_live"}
REPLACED_EXTRA_KEYS = {
    "replaced_holder_session_id", "replaced_holder_name", "replaced_nominated_at",
    "replaced_live_reason",
}
ENTRY_EVIDENCE_PENDING_KEYS = {"status", "prompt_id", "claimed_at"}
WHO_EXTRA_KEYS = {"live", "live_reason", "live_state", "entry_status"}
STANDING_EXTRA_KEYS = {"standing"}
HOLD_KEYS = {"set_by_session", "reason", "set_at", "expires_at"}
SUITE_GRANT_KEYS = {
    "schema_version", "kind", "issuer_session_id", "issuer_name", "grantee_session_id",
    "grantee_name", "scope_root", "issued_at", "expires_at", "note",
}


def _row(sid, name, cwd):
    return session_registry.RegistryRow(session_id=sid, name=name, pid=1, cwd=cwd, status="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = tmp_path / "state" / "group-em"
    records.mkdir(parents=True)
    monkeypatch.setattr(nomination, "settings_home", lambda: tmp_path)
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(True, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr(
        session_registry,
        "liveness_annotation",
        lambda record, registry_dir=None: (True, "live", "live"),
    )
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            _row(HOLDER, "holder-name", str(repo)),
            _row(PEER, "peer-name", str(repo)),
        ],
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    return {"repo": str(repo), "dir": records, "tmp": tmp_path}


def _claim(env, sid, **kw):
    return nomination.claim(env["repo"], sid, directory=env["dir"], prompt_id="p-1", **kw)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_nomination_and_box_holder_records_share_one_key_set(env):
    _claim(env, HOLDER)
    repo_record = _read(nomination._record_path(env["repo"], env["dir"]))
    box_record = _read(env["dir"] / "box" / "holder.json")
    assert set(repo_record) == NOMINATION_KEYS
    assert set(box_record) == NOMINATION_KEYS
    assert set(repo_record["entry_evidence"]) == ENTRY_EVIDENCE_PENDING_KEYS


def test_displacing_claim_adds_displaced_keys_and_a_dead_incumbent_adds_replaced_keys(env, monkeypatch):
    _claim(env, HOLDER)
    _claim(env, PEER)
    live_displaced = _read(nomination._record_path(env["repo"], env["dir"]))
    assert set(live_displaced) == NOMINATION_KEYS | DISPLACED_EXTRA_KEYS

    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(False, "pid_not_running")
    )
    _claim(env, HOLDER)
    replaced = _read(nomination._record_path(env["repo"], env["dir"]))
    assert set(replaced) == NOMINATION_KEYS | DISPLACED_EXTRA_KEYS | REPLACED_EXTRA_KEYS


def test_who_json_keys_are_the_record_plus_liveness_annotation(env):
    _claim(env, HOLDER)
    assert set(standing.who(env["repo"])) == NOMINATION_KEYS | WHO_EXTRA_KEYS
    assert standing.who(str(env["tmp"] / "no-such-repo")) is None


def test_standing_json_keys_add_the_standing_verdict(env):
    _claim(env, HOLDER)
    held = standing.standing(env["repo"], HOLDER)
    assert set(held) == NOMINATION_KEYS | WHO_EXTRA_KEYS | STANDING_EXTRA_KEYS
    assert held["standing"] == "live"
    assert standing.standing(env["repo"], "someone-else")["standing"] == "no_match"


def test_box_hold_record_key_set(env):
    box_hold.set_hold(HOLDER, "reindex", 60, directory=env["dir"], now=NOW)
    assert set(_read(env["dir"] / "box" / "hold.json")) == HOLD_KEYS


def test_suite_grant_record_key_set_and_path(env):
    _claim(env, HOLDER)
    result = suite_grant.issue_suite_grant(
        PEER, "ruled: one broad run", directory=env["dir"], now=NOW
    )
    assert result.ok, result.reason
    path = env["dir"] / "suite-grants" / HOLDER / f"{PEER}.json"
    assert set(_read(path)) == SUITE_GRANT_KEYS
