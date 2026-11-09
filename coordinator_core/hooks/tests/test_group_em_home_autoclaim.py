"""SessionStart home-boot autoclaim: home cwd claims home-start; outside the home writes nothing."""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import nomination
from coordinator_core.hooks import group_em_home_autoclaim as m
from coordinator_core.ops import group_em_enter as gee

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "claude"
    (config / "projects" / "proj-a").mkdir(parents=True)
    settings = tmp_path / "settings"
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    for key in ("MACHINE_LOCAL_COORDINATOR_GROUP_EM_HOME", "MACHINE_LOCAL_COORDINATOR_GENERAL_WORK_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(nomination, "_session_is_real", lambda sid: True)
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_read_pass, "build_roster", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_send_pass, "build_send_digest", lambda *a, **k: {"entries": []})
    monkeypatch.setattr(
        gee.group_em_baseline, "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    home = settings / "group-em"
    home.mkdir(parents=True)
    return config, home


def _transcript(config, cwd, sid=SID):
    line = json.dumps({
        "type": "attachment", "uuid": "u-1", "timestamp": "2026-10-21T00:00:05Z",
        "cwd": str(cwd), "entrypoint": "cli",
    })
    (config / "projects" / "proj-a" / f"{sid}.jsonl").write_text(line + "\n", encoding="utf-8")


def _payload(cwd, session_id=SID, **extra):
    return {"session_id": session_id, "cwd": str(cwd), "source": "startup", **extra}


def _context(out):
    return (out.get("hookSpecificOutput") or {}).get("additionalContext")


def test_home_boot_claims_home_start(env):
    config, home = env
    _transcript(config, home)

    out = m._handler(_payload(home))

    record = nomination.read_record(str(home.resolve()))
    assert record["session_id"] == SID
    assert record["entered_via"] == "home-start"
    assert record["entry_evidence"]["kind"] == "home-start"
    assert "Group EM ACTIVE" in _context(out)


def test_home_subdirectory_boot_claims(env):
    config, home = env
    sub = home / "inbox"
    sub.mkdir()
    _transcript(config, sub)

    m._handler(_payload(sub))

    assert nomination.read_record(str(sub.resolve()))["entry_evidence"]["kind"] == "home-start"


def test_claim_before_transcript_exists_stands_pending(env):
    _config, home = env

    m._handler(_payload(home))

    record = nomination.read_record(str(home.resolve()))
    assert record["entry_evidence"]["kind"] == "home-start"
    assert nomination.entry_status(record)["status"] == "pending"


def test_outside_home_writes_no_claim(env, tmp_path):
    config, _home = env
    repo = tmp_path / "repo"
    repo.mkdir()
    _transcript(config, repo)

    out = m._handler(_payload(repo))

    assert _context(out) is None
    assert nomination.read_record(str(repo.resolve())) is None


def test_compact_source_is_silent(env):
    config, home = env
    _transcript(config, home)

    out = m._handler(_payload(home, source="compact"))

    assert _context(out) is None
    assert nomination.read_record(str(home.resolve())) is None


def test_missing_session_id_is_silent(env):
    _config, home = env

    out = m._handler({"cwd": str(home), "source": "startup"})

    assert _context(out) is None
    assert nomination.read_record(str(home.resolve())) is None


def test_refuted_transcript_refusal_is_reported_and_writes_nothing(env, tmp_path):
    config, home = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _transcript(config, elsewhere)

    out = m._handler(_payload(home))

    assert "NOT CLAIMED" in _context(out)
    assert nomination.read_record(str(home.resolve())) is None


def test_op_call_carries_payload_session_and_only_home_start_evidence(env, monkeypatch):
    _config, home = env
    monkeypatch.setenv("CLAUDE_SESSION_ID", "ambient-session-id")
    calls = []

    def spy(op, params, repo_root=None):
        calls.append((op, params))
        return {}

    monkeypatch.setattr(m, "reentrant_dispatch", spy)

    m._handler(_payload(home))

    assert calls == [("groupem.enter", {
        "repo_root": str(home), "caller_session_id": SID, "entry_evidence": "home-start",
    })]


def test_engine_failure_degrades_to_silence(env, monkeypatch):
    _config, home = env

    def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(m, "reentrant_dispatch", boom)

    assert _context(m._handler(_payload(home))) is None
