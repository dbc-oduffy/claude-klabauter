"""Group EM standing is claimed with a prompt_id and verified at read time from the transcript."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.group_em import human_entry, nomination
from coordinator_core.ops import group_em_enter as gee

SID = "11111111-2222-3333-4444-555555555555"
PID = "prompt-aaaa"
TAG = "<command-message>coordinator:group-em</command-message>\n<command-name>/coordinator:group-em</command-name>"


def _line(**over):
    entry = {
        "type": "user", "uuid": "u-1", "timestamp": "2026-10-06T00:00:00Z", "promptId": PID,
        "origin": {"kind": "human"}, "turnOrigin": "human", "message": {"role": "user", "content": TAG},
    }
    entry.update(over)
    return json.dumps(entry)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "projects" / "proj-a").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h / ".claude"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    return h


def _write(home, *lines, project="proj-a", sid=SID):
    d = home / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stub_legs(monkeypatch):
    monkeypatch.setattr(gee.group_em_read_pass, "fetch_live_agents", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_read_pass, "build_roster", lambda *a, **k: [])
    monkeypatch.setattr(gee.group_em_send_pass, "build_send_digest", lambda *a, **k: {"entries": []})
    monkeypatch.setattr(
        gee.group_em_baseline, "diff_and_persist",
        lambda *a, **k: {"spawned": [], "exited": [], "changed": [], "first_tick": True},
    )


def _enter(repo, **extra):
    return gee._group_em_enter({"repo_root": str(repo), "caller_session_id": SID, **extra})


def _status(repo, now=None):
    return nomination.entry_status(nomination.read_record(str(repo)), now)["status"]


def test_hook_shaped_claim_is_pending_then_verified_once_line_lands(home, tmp_path, monkeypatch):
    _stub_legs(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _enter(repo, prompt_id=PID)

    assert result["standing"]["claimed"] is True
    record = nomination.who(str(repo))
    assert record["entered_via"] == "human-slash-command"
    assert record["entry_evidence"]["status"] == "pending"
    assert record["entry_evidence"]["prompt_id"] == PID
    assert record["entry_status"] == "pending"
    assert nomination.read_authoritative(str(repo)) is None

    _write(home, _line())
    assert nomination.who(str(repo))["entry_status"] == "verified"
    assert nomination.read_authoritative(str(repo))["session_id"] == SID


def test_no_matching_entry_after_window_is_rejected(home, tmp_path, monkeypatch):
    _stub_legs(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _enter(repo, prompt_id=PID)
    assert _status(repo) == "pending"
    assert _status(repo, time.time() + 61) == "rejected"
    assert nomination.read_authoritative(str(repo), now=time.time() + 61) is None


def test_forged_prompt_id_without_human_entry_never_verifies(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    nomination.claim(str(repo), SID, prompt_id="forged")
    _write(home, _line())  # human entry exists, but carries a different promptId
    assert _status(repo) == "pending"
    assert _status(repo, time.time() + 120) == "rejected"


@pytest.mark.parametrize(
    "line",
    [
        _line(origin={"kind": "model"}, turnOrigin="model"),
        _line(origin={"kind": "peer"}, turnOrigin=None),
        _line(origin=None, turnOrigin=None),
        _line(message={"role": "user", "content": [{"type": "text", "text": TAG}]}),
        _line(type="assistant"),
        _line(message={"role": "user", "content": "Base directory for skill\n/group-em</command-name>"}),
    ],
)
def test_matching_prompt_id_but_non_human_entry_is_rejected(home, tmp_path, line):
    repo = tmp_path / "repo"
    repo.mkdir()
    nomination.claim(str(repo), SID, prompt_id=PID)
    _write(home, line)
    assert _status(repo, time.time() + 120) == "rejected"


def test_claim_without_prompt_id_refuses(home, tmp_path, monkeypatch):
    _stub_legs(monkeypatch)
    _write(home, _line())
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _enter(repo)

    assert result["refusal"] == "GROUP-EM-NOT-HUMAN-ENTERED"
    assert result["exit_code"] == gee.EXIT_NOT_HUMAN_ENTERED
    assert result["standing"]["claimed"] is False
    assert "roster" not in result
    assert nomination.who(str(repo)) is None


def test_caller_cannot_point_at_a_forged_transcript(home, tmp_path):
    forged = tmp_path / "forged.jsonl"
    forged.write_text(_line() + "\n", encoding="utf-8")
    nomination.claim(str(tmp_path), SID, prompt_id=PID)
    record = nomination.read_record(str(tmp_path))
    record["entry_evidence"]["transcript"] = str(forged)
    assert human_entry.resolve_entry_evidence(record, time.time() + 120)["status"] == "rejected"


def test_bare_verb_form_is_found(home):
    _write(home, _line(message={"role": "user", "content": "<command-name>/group-em</command-name>"}))
    assert human_entry.find_human_entry(SID, prompt_id=PID)["uuid"] == "u-1"


@pytest.mark.parametrize("bad", ["../x", "..\\x", "a/b", SID + "/..", "", "*", "a?c", "[ab]"])
def test_traversal_and_glob_session_ids_refused(home, tmp_path, bad):
    (home / ".claude" / "x.jsonl").write_text(_line() + "\n", encoding="utf-8")
    assert human_entry.find_human_entry(bad) is None
    with pytest.raises(nomination.NotHumanEnteredError):
        nomination.claim(str(tmp_path), bad, prompt_id=PID)


def test_scan_is_fast_on_a_large_transcript(home):
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 2000}})
    _write(home, *([filler] * 5000), _line())

    assert human_entry.find_human_entry(SID, prompt_id=PID) is not None
    scan = in_process_time_ms(lambda: human_entry.find_human_entry(SID, prompt_id=PID))
    assert scan["process_time_ms"] < 500


def _legacy_record(repo, session_id=SID, evidence=None):
    record = {"version": 1, "repo_root": str(repo), "session_id": session_id, "peer_name": None,
              "nominated_at": "2026-09-02T17:12:05Z", "nominated_by": None}
    if evidence is not None:
        record["entered_via"] = "human-slash-command"
        record["entry_evidence"] = evidence
    nomination._write_json_atomic(nomination._record_path(str(repo)), record)


@pytest.mark.parametrize("evidence", [None, {"uuid": "u-0", "transcript": "x", "timestamp": "t"}])
def test_legacy_record_verifies_on_any_human_entry_in_own_transcript(home, tmp_path, evidence):
    repo = tmp_path / "repo"
    repo.mkdir()
    _legacy_record(repo, evidence=evidence)
    _write(home, _line(promptId="some-other-prompt"))
    assert nomination.read_authoritative(str(repo))["session_id"] == SID
    assert nomination.who(str(repo))["entry_status"] == "verified"


def test_legacy_record_without_human_entry_is_rejected(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _legacy_record(repo)
    _write(home, _line(origin={"kind": "model"}))
    assert nomination.read_authoritative(str(repo)) is None
    assert nomination.who(str(repo))["entry_status"] == "rejected"


def test_final_verdict_is_served_from_cache_without_a_scan(home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    nomination.claim(str(repo), SID, prompt_id=PID)
    _write(home, _line())
    assert _status(repo) == "verified"

    def _no_scan(*_a, **_k):
        raise AssertionError("verified standing re-scanned the transcript")

    monkeypatch.setattr(human_entry, "_scan_transcripts", _no_scan)
    assert _status(repo) == "verified"
    assert nomination.read_authoritative(str(repo))["session_id"] == SID


def test_pending_rescans_only_the_appended_tail(home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    nomination.claim(str(repo), SID, prompt_id=PID)
    filler = json.dumps({"type": "assistant", "message": {"content": "/group-em</command-name>"}})
    _write(home, filler, filler)
    assert _status(repo) == "pending"

    seen = []
    real = human_entry._scan_transcripts

    def _spy(session_id, projects_root, prompt_id, offsets):
        seen.append(dict(offsets))
        return real(session_id, projects_root, prompt_id, offsets)

    monkeypatch.setattr(human_entry, "_scan_transcripts", _spy)
    path = home / ".claude" / "projects" / "proj-a" / f"{SID}.jsonl"
    scanned = path.stat().st_size
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_line() + "\n")
    assert _status(repo) == "verified"
    assert list(seen[0].values()) == [scanned]


def test_a_new_claim_misses_the_prior_claims_cached_verdict(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    nomination.claim(str(repo), SID, prompt_id=PID)
    _write(home, _line())
    assert _status(repo) == "verified"
    nomination.claim(str(repo), SID, prompt_id="forged-second")
    assert _status(repo, time.time() + 120) == "rejected"
