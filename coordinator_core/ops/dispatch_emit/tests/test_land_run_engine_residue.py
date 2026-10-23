"""dispatch.land_run's reply lists dirty engine-owned paths outside the landing as advisory engine_residue."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, land_run_contract
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000c2"
RUN = "plan-20261023T100000-c2c2c2"
SCRIPT = "run-c2.mjs"
OWN = "docs/own.md"
RESIDUE = "state/apm-rulings/r.yaml"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, OWN, "own\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _land(repo: Path) -> dict:
    base = _git(["rev-parse", "HEAD"], repo).strip()
    _write(repo, OWN, "own\nedited\n")
    session_scope.touch(SELF, OWN, cwd=str(repo))
    sink = touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=OWN,
        content_hash=touch_record.compute_content_hash(repo / OWN), kind="w",
    )
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="t", paths=(OWN,)),), plan_path=None)
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN, "run_base_sha": base},
        repo_root=repo / ".git",
    )


def test_engine_residue_is_a_reply_key():
    assert "engine_residue" in land_run_contract.REPLY_KEYS


def test_dirty_apm_ruling_outside_the_landing_is_listed_and_never_an_error(repo):
    _write(repo, RESIDUE, "ruling: x\n")

    out = _land(repo)

    assert out.get("committed") is True, out
    assert not out.get("error")
    assert out["engine_residue"] == [RESIDUE]


def test_control_clean_tree_carries_an_empty_list(repo):
    out = _land(repo)

    assert out.get("committed") is True, out
    assert out["engine_residue"] == []


def test_a_non_engine_dirty_path_is_not_listed(repo):
    _write(repo, "src/other.py", "x = 1\n")

    out = _land(repo)

    assert out["engine_residue"] == []


def test_residue_adds_no_process_spawns(repo, monkeypatch, tmp_path):
    spawns = {"n": 0}
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            spawns["n"] += 1
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    _write(repo, RESIDUE, "ruling: x\n")
    with_residue = _land(repo)
    n_with = spawns["n"]
    assert with_residue["engine_residue"] == [RESIDUE]

    other = tmp_path / "other"
    other.mkdir()
    clean = _fresh_repo(other)
    spawns["n"] = 0
    without = _land(clean)
    assert without["engine_residue"] == []
    assert n_with == spawns["n"]


def _fresh_repo(root: Path) -> Path:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, OWN, "own\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    session_core.init(SELF, cwd=str(root))
    return root
