"""`commit_v2` carries the session's stopped-run residue by default and reports `stopped_run_residue`."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "srr-self"
PEER = "srr-peer"
SCRIPT = "scratch/warp/x.mjs"
SIZING = "state/sizings/a.yaml"
BATON = "state/handoffs/a.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _run_id(suffix: str) -> str:
    stamp = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ask-{stamp}-{suffix}"


def _launch(tool_use_id: str, task: str) -> str:
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        "toolUseResult": {"status": "async_launched", "taskId": task},
    }
    return json.dumps(obj) + "\n"


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _submit(repo: Path, tmp_path: Path, run_id: str, session: str, *, stopped: bool) -> None:
    body = _launch("tu-" + run_id, "w1") + ("Successfully stopped task: w1\n" if stopped else "")
    transcript = tmp_path / f"t-{run_id}.jsonl"
    transcript.write_text(body, encoding="utf-8")
    run_token.mint(repo, run_id, SCRIPT, session_id=session, sizing_rel=None)
    warp_live.record_submission(
        repo, run_id, session_id=session, transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=None, tool_use_id="tu-" + run_id,
    )


def _artifacts(repo: Path, run_id: str, sizing: str, baton: str) -> None:
    _write(repo, f"state/warp/{run_id}/run.json", json.dumps({"sizing_rel": sizing}))
    _write(repo, f"state/warp/{run_id}/requirements.md")
    _write(repo, sizing, f"baton: {baton}\n")
    _write(repo, baton, "---\ncarried_items: []\n---\nbody\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    return root


@pytest.fixture
def incident(repo, tmp_path):
    """A stopped own run, a still-live own run and a stopped peer run, all with dirty artifacts."""
    stopped, live, peer = _run_id("aaaaaa"), _run_id("bbbbbb"), _run_id("cccccc")
    _submit(repo, tmp_path, stopped, SELF, stopped=True)
    _artifacts(repo, stopped, SIZING, BATON)
    _submit(repo, tmp_path, live, SELF, stopped=False)
    _write(repo, f"state/warp/{live}/requirements.md")
    _submit(repo, tmp_path, peer, PEER, stopped=True)
    _artifacts(repo, peer, "state/sizings/peer.yaml", "state/handoffs/peer.md")
    return stopped, live, peer


def _dirty(repo: Path) -> set:
    return {ln[3:] for ln in _git(["status", "--porcelain", "-uall"], repo).splitlines()}


def _landed(repo: Path) -> set:
    names = set()
    for rev in ("HEAD", "HEAD~1"):
        names |= set(_git(["show", "--name-only", "--format=", rev], repo).split())
    return names


def test_unrelated_commit_lands_the_stopped_run_residue(repo, incident):
    stopped, live, peer = incident

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True, result
    carried = result["stopped_run_residue"]["carried"]
    assert list(carried) == [stopped]
    assert set(carried[stopped]) == {
        SIZING, BATON, f"state/warp/{stopped}/requirements.md", f"state/warp/{stopped}/run.json",
    }
    assert result["stopped_run_residue"]["withheld"] == {}
    assert f"carried stopped run {stopped}'s residue: {len(carried[stopped])} file(s)" in result["warnings"]
    assert set(carried[stopped]) <= _landed(repo)
    log = _git(["log", "-2", "--format=%B"], repo)
    assert f"Stopped-Run-Residue: {stopped}" in log


def test_live_own_run_and_peer_run_stay_dirty(repo, incident):
    stopped, live, peer = incident

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    dirty = _dirty(repo)
    assert f"state/warp/{live}/requirements.md" in dirty
    assert f"state/warp/{peer}/requirements.md" in dirty
    assert "state/sizings/peer.yaml" in dirty
    reported = {p for ps in result["stopped_run_residue"]["carried"].values() for p in ps}
    assert not any(live in p or peer in p or "peer" in p for p in reported)


def test_opt_out_carries_nothing_and_omits_the_key(repo, incident):
    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_stopped_runs": False})

    assert result["committed"] is True
    assert "stopped_run_residue" not in result
    assert SIZING in _dirty(repo)
    assert "Stopped-Run-Residue" not in _git(["log", "-2", "--format=%B"], repo)


def test_non_bool_flag_is_refused(repo):
    result = _call(repo, {"paths": ["a.py"], "message": "m", "carry_stopped_runs": "yes"})

    assert result["committed"] is False
    assert "carry_stopped_runs" in result["error"]


def test_no_stopped_run_means_no_key_and_no_extra_spawn(repo, tmp_path, monkeypatch):
    live = _run_id("dddddd")
    _submit(repo, tmp_path, live, SELF, stopped=False)
    _write(repo, f"state/warp/{live}/requirements.md")
    spawns = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            spawns.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)

    on = _call(repo, {"paths": ["a.py"], "message": "m"})
    on_spawns = len(spawns)
    (repo / "a.py").write_text("v3\n", encoding="utf-8")
    spawns.clear()
    off = _call(repo, {"paths": ["a.py"], "message": "m", "carry_stopped_runs": False})

    assert "stopped_run_residue" not in on
    assert "stopped_run_residue" not in off
    assert on_spawns == len(spawns)


def test_residue_failure_degrades_to_no_carry(repo, incident, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stopped_runs

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(stopped_runs, "residue", boom)

    result = _call(repo, {"paths": ["a.py"], "message": "m"})

    assert result["committed"] is True
    assert "stopped_run_residue" not in result
    assert any("stopped-run residue unavailable" in w for w in result["warnings"])
