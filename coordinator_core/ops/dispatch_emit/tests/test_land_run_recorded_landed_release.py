"""A peer run recorded landed (or halted with a shipped sizing) holds none of its declared paths
against a land_run landing; a live unlanded peer still does."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a2"
RUN = "plan-20261021T100000-aaaaaa"
SCRIPT = "run-a.mjs"
SIZING = "state/sizings/peer.yaml"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
PEER = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-28d714"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _author(root: Path, rel: str, text: str) -> None:
    _write(root, rel, text)
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _peer(root: Path, marker: str | None, sizing_status: str = "open") -> None:
    rec = f"{RECORD_DIR_ROOT}/{PEER}"
    _write(root, f"{rec}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, f"{rec}/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": ["other/two.py"]}]}))
    _write(root, SIZING, f"status: {sizing_status}\n")
    if marker:
        _write(root, f"{rec}/halted.json", json.dumps({"status": marker, "sizing_rel": SIZING, "sha": "abc1234"}))
    run_token.mint(root, PEER, "b.mjs", session_id=SELF)


def _own_run(root: Path) -> None:
    _author(root, "src/one.py", "one = 1\n")
    _author(root, "other/two.py", "two = 2\n")
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="row one", paths=("src/one.py",)),
            ChunkCommit(id="C2", title="row two", paths=("other/two.py",)),
        ),
        plan_path=None,
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")


def _land(root: Path) -> dict:
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(REVIEWED),
         "session_id": SELF, "run_id": RUN},
        repo_root=root / ".git",
    )


def _tree(root: Path) -> set:
    return set(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def test_peer_recorded_landed_releases_its_declared_path(repo):
    _peer(repo, "landed")
    _own_run(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert {"src/one.py", "other/two.py"} <= _tree(repo)
    assert out["stranded"] == {}


def test_halted_peer_with_shipped_sizing_releases_its_declared_path(repo):
    _peer(repo, "halted", sizing_status="shipped")
    _own_run(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert "other/two.py" in _tree(repo)


def test_live_unlanded_peer_still_holds_its_declared_path(repo):
    _peer(repo, None)
    _own_run(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert "src/one.py" in _tree(repo) and "other/two.py" not in _tree(repo)
    assert out["stranded"] == {"C2": ["other/two.py"]}
