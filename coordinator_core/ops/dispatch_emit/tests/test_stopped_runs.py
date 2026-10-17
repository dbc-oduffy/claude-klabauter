"""stopped_runs: a session's ended runs and their dirty, uncontested artifacts."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token, stopped_runs
from coordinator_core.session import scope

SCRIPT = "scratch/warp/x.mjs"
SESSION = "s1"


def _run_id(age_seconds: float = 60, suffix: str = "abc123") -> str:
    stamp = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ask-{stamp}-{suffix}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    return root


def _launch_line(tool_use_id, task):
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        "toolUseResult": {"status": "async_launched", "taskId": task},
    }
    return json.dumps(obj) + "\n"


STOPPED = "Successfully stopped task: {task}\n"


def _submit(repo, tmp_path, run_id, *, session=SESSION, body, token=True, name="t"):
    transcript = tmp_path / f"{name}-{run_id}.jsonl"
    transcript.write_text(body, encoding="utf-8")
    if token:
        run_token.mint(repo, run_id, SCRIPT, session_id=session, sizing_rel=None)
    warp_live.record_submission(
        repo, run_id, session_id=session, transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=None, tool_use_id="tu-" + run_id,
    )
    return transcript


def _stopped_body(run_id, task="w1"):
    return _launch_line("tu-" + run_id, task) + STOPPED.format(task=task)


def _live_body(run_id, task="w1"):
    return _launch_line("tu-" + run_id, task)


def test_held_token_and_stop_notification_is_stopped(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    assert stopped_runs.stopped_own_runs(repo, SESSION) == (rid,)


def test_tokenless_run_with_stop_notification_is_stopped(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid), token=False)
    assert stopped_runs.stopped_own_runs(repo, SESSION) == (rid,)


def test_tokenless_run_without_terminal_notification_is_not_stopped(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_live_body(rid), token=False)
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_live_run_is_not_stopped(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_live_body(rid))
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_claimed_token_is_live(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    path = run_token._record_path(repo, rid)
    claimed = run_token._record_path(repo, rid, claimed=True)
    claimed.parent.mkdir(parents=True, exist_ok=True)
    path.replace(claimed)
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_other_sessions_submission_is_not_ours(repo, tmp_path):
    rid = _run_id()
    _submit(repo, tmp_path, rid, session="s2", body=_stopped_body(rid))
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_run_older_than_the_window_is_not_scanned(repo, tmp_path):
    rid = _run_id(age_seconds=stopped_runs.STOPPED_RUN_WINDOW_SECONDS + 3600)
    _submit(repo, tmp_path, rid, body=_stopped_body(rid), token=False)
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_window_equals_token_ttl():
    assert stopped_runs.STOPPED_RUN_WINDOW_SECONDS == run_token.TTL_SECONDS


def test_timer_end_is_not_evidence(repo, tmp_path):
    rid = _run_id(age_seconds=3 * 3600)
    _submit(repo, tmp_path, rid, body="", token=False)
    assert stopped_runs.stopped_own_runs(repo, SESSION) == ()


def test_run_ended_on_evidence_is_false_for_timer(repo, tmp_path):
    record = {"run_id": "r", "script_path": SCRIPT, "minted_at": time.time() - 7200, "session_id": SESSION}
    assert warp_live.run_ended_on_evidence(repo, record) is False


def _write(repo, rel, text="x\n"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _artifacts(repo, rid, sizing="state/sizings/a.yaml"):
    _write(repo, f"state/warp/{rid}/run.json", json.dumps({"sizing_rel": sizing}))
    _write(repo, f"state/warp/{rid}/requirements.md")
    _write(repo, sizing, "baton: state/handoffs/a.md\n")
    _write(repo, "state/handoffs/a.md")


@pytest.fixture
def no_peers(monkeypatch):
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})


def test_residue_carries_dirty_candidates_outside_pathspec(repo, tmp_path, no_peers):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    _artifacts(repo, rid)
    got = stopped_runs.residue(repo, SESSION, ["other.txt", "state\\handoffs\\a.md"])
    assert got.withheld == {}
    assert got.carried == {
        rid: ("state/sizings/a.yaml", f"state/warp/{rid}/requirements.md", f"state/warp/{rid}/run.json")
    }


def test_residue_withholds_peer_held_paths(repo, tmp_path, monkeypatch):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    _artifacts(repo, rid)
    held = "state/sizings/a.yaml"
    monkeypatch.setattr(
        scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {held: ["peer"]} if held in paths else {}
    )
    got = stopped_runs.residue(repo, SESSION, [])
    assert got.withheld == {held: "peer-held"}
    assert held not in got.carried[rid]


def test_residue_unknown_contest_carries_nothing(repo, tmp_path, monkeypatch):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    _artifacts(repo, rid)
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda paths, sid, cwd=None: None)
    got = stopped_runs.residue(repo, SESSION, [])
    assert got.carried == {}
    assert set(got.withheld.values()) == {"contest-unknown"}
    assert "state/sizings/a.yaml" in got.withheld


def test_residue_ignores_live_run(repo, tmp_path, no_peers):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_live_body(rid))
    _artifacts(repo, rid)
    assert stopped_runs.residue(repo, SESSION, []) == stopped_runs.StoppedResidue({}, {})


def test_zero_subprocess(repo, tmp_path, no_peers, monkeypatch):
    rid = _run_id()
    _submit(repo, tmp_path, rid, body=_stopped_body(rid))
    _artifacts(repo, rid)

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert stopped_runs.residue(repo, SESSION, []).carried
