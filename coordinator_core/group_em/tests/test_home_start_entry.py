"""The home-start evidence kind: verified from the session's own transcript start, never a caller."""

from __future__ import annotations

import json

import pytest

from coordinator_core.group_em import human_entry

SID = "11111111-2222-3333-4444-555555555555"
CLAIMED = "2026-10-21T00:00:00+00:00"
CLAIMED_TS = human_entry._parse_iso(CLAIMED)
HOME = "/the/home"


def _cwd_line(cwd=HOME, ts="2026-10-21T00:00:05Z", entrypoint="cli", uuid="u-1"):
    return json.dumps(
        {"type": "attachment", "uuid": uuid, "timestamp": ts, "cwd": cwd, "entrypoint": entrypoint}
    )


PRELUDE = [
    json.dumps({"type": "last-prompt", "sessionId": SID}),
    json.dumps({"type": "mode", "mode": "normal"}),
]


def _write(root, *lines, project="proj-a", sid=SID):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _record(claimed_at=CLAIMED, sid=SID):
    return {
        "session_id": sid,
        "entry_evidence": {"kind": human_entry.HOME_START, "claimed_at": claimed_at},
    }


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(human_entry, "_in_home", lambda cwd: cwd.startswith(HOME))
    return root


def _verdict(projects, record=None, now=CLAIMED_TS + 10, cache=None):
    return human_entry.resolve_entry_evidence(
        record or _record(), now, projects_root=projects, cache_path=cache
    )


def test_earliest_cwd_entry_skips_cwdless_lines(projects):
    _write(projects, *PRELUDE, _cwd_line())
    entry = human_entry.earliest_cwd_entry(SID, projects)
    assert entry["cwd"] == HOME and entry["entrypoint"] == "cli" and entry["uuid"] == "u-1"


def test_scan_stops_at_first_cwd_line(projects):
    _write(projects, *PRELUDE, _cwd_line(), _cwd_line(cwd="/elsewhere", uuid="u-2"))
    assert human_entry.earliest_cwd_entry(SID, projects)["cwd"] == HOME


def test_earlier_timestamp_wins_across_project_dirs(projects):
    _write(projects, _cwd_line(cwd="/late", ts="2026-10-21T00:00:09Z"), project="a")
    _write(projects, _cwd_line(cwd=HOME, ts="2026-10-21T00:00:01Z"), project="b")
    assert human_entry.earliest_cwd_entry(SID, projects)["cwd"] == HOME


def test_no_entry_is_none(projects):
    _write(projects, *PRELUDE)
    assert human_entry.earliest_cwd_entry(SID, projects) is None


def test_inside_home_cli_verifies(projects):
    _write(projects, *PRELUDE, _cwd_line())
    verdict = _verdict(projects)
    assert verdict["status"] == "verified"
    assert verdict["kind"] == human_entry.HOME_START and verdict["cwd"] == HOME


def test_outside_home_rejects(projects):
    _write(projects, _cwd_line(cwd="/elsewhere"))
    assert _verdict(projects) == {
        "status": "rejected", "reason": "cwd-outside-home",
        "kind": human_entry.HOME_START, "claimed_at": CLAIMED,
    }


def test_sdk_cli_rejects_not_interactive(projects):
    _write(projects, _cwd_line(entrypoint="sdk-cli"))
    assert _verdict(projects)["reason"] == "not-interactive"


def test_home_unresolved_rejects(projects, monkeypatch):
    monkeypatch.setattr(human_entry, "_in_home", lambda cwd: None)
    _write(projects, _cwd_line())
    assert _verdict(projects)["reason"] == "home-unresolved"


def test_no_entry_pending_then_rejected(projects):
    assert _verdict(projects, now=CLAIMED_TS + 10)["status"] == "pending"
    verdict = _verdict(projects, now=CLAIMED_TS + 120)
    assert verdict["status"] == "rejected" and verdict["reason"] == "no-start-entry"


def test_pending_becomes_verified_once_line_appears(projects, tmp_path):
    cache = tmp_path / "cache.json"
    assert _verdict(projects, cache=cache)["status"] == "pending"
    _write(projects, _cwd_line())
    assert _verdict(projects, cache=cache)["status"] == "verified"


def test_final_verdict_served_from_cache_without_transcript(projects, tmp_path):
    cache = tmp_path / "cache.json"
    path = _write(projects, _cwd_line())
    assert _verdict(projects, cache=cache)["status"] == "verified"
    path.unlink()
    assert _verdict(projects, cache=cache)["status"] == "verified"


def test_human_group_em_line_alone_never_verifies_home_start(projects):
    tag = "<command-name>/group-em</command-name>"
    line = json.dumps({
        "type": "user", "uuid": "u-1", "timestamp": "2026-10-21T00:00:01Z", "promptId": "p-1",
        "origin": {"kind": "human"}, "message": {"role": "user", "content": tag},
    })
    _write(projects, line)
    assert _verdict(projects, now=CLAIMED_TS + 120)["status"] == "rejected"


def test_prompt_id_cache_without_kind_still_reads_back(projects, tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"session_id": SID, "prompt_id": "p-1", "offsets": {}, "status": "verified",
                    "uuid": "u"}),
        encoding="utf-8",
    )
    record = {"session_id": SID, "entry_evidence": {"prompt_id": "p-1", "claimed_at": CLAIMED}}
    assert _verdict(projects, record=record, cache=cache) == {
        "status": "verified", "prompt_id": "p-1", "uuid": "u"
    }


def test_reclaim_after_cached_rejection_is_judged_afresh(projects, tmp_path):
    cache = tmp_path / "cache.json"
    assert _verdict(projects, now=CLAIMED_TS + 120, cache=cache)["status"] == "rejected"
    _write(projects, _cwd_line())
    later = "2026-10-21T00:05:00+00:00"
    verdict = _verdict(
        projects, record=_record(claimed_at=later), now=human_entry._parse_iso(later) + 1, cache=cache
    )
    assert verdict["status"] == "verified"


def test_invalid_session_id_rejected(projects):
    verdict = human_entry.resolve_entry_evidence(_record(sid="../x"), CLAIMED_TS, projects)
    assert verdict == {"status": "rejected", "reason": "bad-session-id"}


def test_home_start_refusal(projects):
    assert human_entry.home_start_refusal("../x", projects) == "bad-session-id"
    assert human_entry.home_start_refusal(SID, projects) is None
    _write(projects, _cwd_line(cwd="/elsewhere"))
    assert human_entry.home_start_refusal(SID, projects) == "cwd-outside-home"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_prompt_id_readers_do_not_import_gem_home():
    import subprocess
    import sys

    code = "import sys; import coordinator_core.group_em.human_entry; sys.exit('coordinator_core.group_em.gem_home' in sys.modules)"
    assert subprocess.run([sys.executable, "-I", "-c", code], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0
