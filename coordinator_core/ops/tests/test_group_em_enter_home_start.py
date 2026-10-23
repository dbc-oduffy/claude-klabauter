"""`groupem.enter` with entry_evidence "home-start": the transcript start, not the caller, decides."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.group_em import human_entry, nomination
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
    for key in ("MACHINE_LOCAL_COORDINATOR_GROUP_EM_HOME", "MACHINE_LOCAL_COORDINATOR_GENERAL_WORK_HOME"):
        monkeypatch.delenv(key, raising=False)
    # nomination.claim refuses a session with no registry row or transcript; the pending-proof
    # cases here claim before any transcript exists, so the real-session check is stubbed.
    monkeypatch.setattr(nomination, "_session_is_real", lambda sid: True)
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_read_pass, "build_roster", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_send_pass, "build_send_digest", lambda *a, **k: {"entries": []})
    monkeypatch.setattr(
        gee.group_em_baseline, "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )
    return config, settings / "group-em"


def _transcript(config, cwd, entrypoint="cli"):
    line = json.dumps({
        "type": "attachment", "uuid": "u-1", "timestamp": "2026-10-21T00:00:05Z",
        "cwd": str(cwd), "entrypoint": entrypoint,
    })
    path = config / "projects" / "proj-a" / f"{SID}.jsonl"
    path.write_text(json.dumps({"type": "mode"}) + "\n" + line + "\n", encoding="utf-8")


def _enter(repo, **extra):
    return gee._group_em_enter({"repo_root": str(repo), "caller_session_id": SID, **extra})


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _assert_refused(result, repo):
    assert result["refusal"] == "GROUP-EM-NOT-HUMAN-ENTERED"
    assert result["exit_code"] == gee.EXIT_NOT_HUMAN_ENTERED
    assert result["standing"]["claimed"] is False
    assert "roster" not in result
    assert nomination.read_record(str(repo)) is None


def test_start_inside_home_claims_and_verifies(env, tmp_path):
    config, home = env
    _transcript(config, home)
    repo = _repo(tmp_path)

    result = _enter(repo, entry_evidence="home-start")

    assert result["standing"]["claimed"] is True
    assert "refusal" not in result and "exit_code" not in result
    record = nomination.read_record(str(repo))
    assert record["entered_via"] == "home-start"
    assert record["entry_evidence"]["kind"] == "home-start"
    assert nomination.entry_status(record)["status"] == "verified"


def test_start_outside_home_refuses_and_writes_nothing(env, tmp_path):
    config, _ = env
    _transcript(config, tmp_path / "elsewhere")
    repo = _repo(tmp_path)
    _assert_refused(_enter(repo, entry_evidence="home-start"), repo)


def test_headless_start_inside_home_refuses(env, tmp_path):
    config, home = env
    _transcript(config, home, entrypoint="sdk-cli")
    repo = _repo(tmp_path)
    _assert_refused(_enter(repo, entry_evidence="home-start"), repo)


def test_no_transcript_yet_is_pending_then_rejected(env, tmp_path):
    repo = _repo(tmp_path)

    result = _enter(repo, entry_evidence="home-start")

    assert result["standing"]["claimed"] is True
    record = nomination.read_record(str(repo))
    assert nomination.entry_status(record, time.time() + 10)["status"] == "pending"
    assert nomination.entry_status(record, time.time() + 120)["status"] == "rejected"


@pytest.mark.parametrize("extra", [
    {"entry_evidence": "home-start", "prompt_id": "p-1"},
    {"entry_evidence": "bogus"},
    {"entry_evidence": "bogus", "prompt_id": "p-1"},
    {},
])
def test_wrong_proof_combinations_refuse(env, tmp_path, extra):
    config, home = env
    _transcript(config, home)
    repo = _repo(tmp_path)
    _assert_refused(_enter(repo, **extra), repo)


def test_transcript_decides_not_repo_root(env, tmp_path):
    config, home = env
    _transcript(config, home)
    outside = _repo(tmp_path)
    assert _enter(outside, entry_evidence="home-start")["standing"]["claimed"] is True

    _transcript(config, tmp_path / "elsewhere")
    inside = home
    inside.mkdir(parents=True)
    _assert_refused(_enter(inside, entry_evidence="home-start"), inside)


def test_home_start_claim_and_cached_status_are_fast(env, tmp_path):
    config, home = env
    _transcript(config, home)
    repo = _repo(tmp_path)

    started = time.process_time()
    result = _enter(repo, entry_evidence="home-start")
    claim_ms = (time.process_time() - started) * 1000
    assert result["standing"]["claimed"] is True
    record = nomination.read_record(str(repo))
    assert nomination.entry_status(record)["status"] == "verified"
    started = time.process_time()
    verdict = nomination.entry_status(record)
    status_ms = (time.process_time() - started) * 1000

    assert verdict["status"] == "verified"
    assert claim_ms < 200 and status_ms < 200


def test_prompt_id_path_keeps_its_record_shape(env, tmp_path):
    repo = _repo(tmp_path)
    assert _enter(repo, prompt_id="p-1")["standing"]["claimed"] is True
    record = nomination.read_record(str(repo))
    assert record["entered_via"] == "human-slash-command"
    assert record["entry_evidence"]["prompt_id"] == "p-1"
    assert human_entry.HOME_START not in json.dumps(record["entry_evidence"])
