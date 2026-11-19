"""warp_live.live_run_for_session: one scan per scope, and a stat-keyed not-live verdict across scopes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.ops.dispatch_emit import run_token

SCRIPT = "scratch/warp/x.mjs"
SESSION = "s1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    warp_live._NOT_LIVE.clear()
    yield
    warp_live._NOT_LIVE.clear()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    return managed, legacy


@pytest.fixture
def calls(monkeypatch):
    """The run ids `_record_live` is asked about, in order."""
    seen: list[str] = []
    real = warp_live._record_live

    def spy(root, record, *args, **kwargs):
        seen.append(record["run_id"])
        return real(root, record, *args, **kwargs)

    monkeypatch.setattr(warp_live, "_record_live", spy)
    return seen


def _submitted(repo, run_id, script=SCRIPT):
    run_token.mint(repo, run_id, script, session_id=SESSION, sizing_rel=None)
    warp_live.record_submission(
        repo, run_id, session_id=SESSION, transcript_path=None, script_path=script, sizing_rel=None
    )


def _past_minted_grace(monkeypatch):
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.UNLAUNCHED_GRACE_SECONDS + 60)


def _launch(dirs, task_id="w1", script=SCRIPT):
    path = Path(launch_records.record_path(str(dirs[0]), SESSION, task_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scriptPath": script, "fired_at": 1.0}), encoding="utf-8")


def _not_live_runs(repo, monkeypatch, count=3):
    for n in range(count):
        _submitted(repo, f"r{n}")
    _past_minted_grace(monkeypatch)


def test_one_scope_scans_the_records_once(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch)
    with warp_live.listing_scope():
        assert warp_live.live_run_for_session(repo, SESSION) is None
        first = len(calls)
        assert first == 3
        assert warp_live.live_run_for_session(repo, SESSION) is None
        assert warp_live.live_run_for_session(repo, SESSION) is None
    assert len(calls) == first


def test_scope_memo_is_keyed_by_session_and_transcript(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    with warp_live.listing_scope():
        warp_live.live_run_for_session(repo, SESSION)
        warp_live.live_run_for_session(repo, SESSION, transcript_path="t.jsonl")
        assert warp_live.live_run_for_session(repo, "other") is None
    assert len(calls) == 2


def test_a_not_live_verdict_is_reused_across_scopes(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch)
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert len(calls) == 3
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert len(calls) == 3


def test_a_live_answer_is_never_carried_across_scopes(repo, dirs, calls):
    _submitted(repo, "r0")
    _launch(dirs)
    assert warp_live.live_run_for_session(repo, SESSION) == "r0"
    assert warp_live.live_run_for_session(repo, SESSION) == "r0"
    assert calls == ["r0", "r0"]
    assert not warp_live._NOT_LIVE


def test_a_changed_submission_reevaluates(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    warp_live.live_run_for_session(repo, SESSION)
    path = warp_live._submission_path(repo, "r0")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["note"] = "changed"
    path.write_text(json.dumps(record), encoding="utf-8")
    warp_live.live_run_for_session(repo, SESSION)
    assert calls == ["r0", "r0"]


def test_a_halted_marker_reevaluates(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    warp_live.live_run_for_session(repo, SESSION)
    marker = warp_live._ask_record_dir(repo, "r0") / warp_live._HALTED_FILE
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"status": "landed"}), encoding="utf-8")
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert calls == ["r0", "r0"]
    assert warp_live._NOT_LIVE


def test_a_new_launch_record_revives_the_run(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    assert warp_live.live_run_for_session(repo, SESSION) is None
    _launch(dirs)
    assert warp_live.live_run_for_session(repo, SESSION) == "r0"
    assert calls == ["r0", "r0"]


def test_a_changed_transcript_reevaluates(repo, dirs, calls, monkeypatch, tmp_path):
    _not_live_runs(repo, monkeypatch, count=1)
    transcript = tmp_path / "t.jsonl"
    warp_live.live_run_for_session(repo, SESSION, transcript_path=str(transcript))
    transcript.write_text("x", encoding="utf-8")
    warp_live.live_run_for_session(repo, SESSION, transcript_path=str(transcript))
    assert calls == ["r0", "r0"]


def test_a_run_stage_change_reevaluates(repo, dirs, calls, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    warp_live.live_run_for_session(repo, SESSION)
    stage = warp_live._ask_record_dir(repo, "r0") / "run-stage.json"
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_text(json.dumps({"stage": "plan"}), encoding="utf-8")
    warp_live.live_run_for_session(repo, SESSION)
    assert calls == ["r0", "r0"]


def test_a_verdict_that_spent_the_token_is_not_cached(repo, dirs, calls, monkeypatch):
    _submitted(repo, "r0")
    monkeypatch.setattr(warp_live, "_owner_exited", lambda *a, **k: True)
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert not run_token.is_live(repo, "r0")
    assert not warp_live._NOT_LIVE


def test_ended_on_evidence_is_never_served_from_the_cache(repo, dirs, monkeypatch):
    _not_live_runs(repo, monkeypatch, count=1)
    assert warp_live.live_run_for_session(repo, SESSION) is None
    assert warp_live._NOT_LIVE
    monkeypatch.setattr(warp_live, "_owner_exited", lambda *a, **k: True)
    record = next(warp_live._token_records(repo))
    assert warp_live.run_ended_on_evidence(repo, record) is True
    assert not run_token.is_live(repo, "r0")


def test_the_cache_is_bounded(repo, dirs, monkeypatch):
    monkeypatch.setattr(warp_live, "_NOT_LIVE_MAX", 2)
    _not_live_runs(repo, monkeypatch, count=4)
    warp_live.live_run_for_session(repo, SESSION)
    assert len(warp_live._NOT_LIVE) == 2
