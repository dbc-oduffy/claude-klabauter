"""Credited-row completeness: every credited row's changed declared path lands, or a named withhold
keeps it out with a Row-Unlanded line in the commit message and an entry in the reply's stranded."""

from __future__ import annotations

import json
import subprocess
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

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
RUN = "plan-20261021T100000-aaaaaa"
RUN_B = "run-b"
SCRIPT = "run-a.mjs"
REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


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


def _two_rows(root: Path) -> None:
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


def _tree(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def _message(root: Path) -> str:
    return _git(["log", "-1", "--format=%B", "HEAD"], root)


def test_both_credited_rows_land_with_no_row_unlanded_line(repo):
    _two_rows(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    assert {"src/one.py", "other/two.py"} <= set(_tree(repo))
    assert "Row-Unlanded" not in _message(repo)
    assert "Lands-Chunks: C1, C2" in _message(repo)
    assert out["stranded"] == {}


def test_a_peer_held_row_path_is_named_and_the_other_row_still_lands(repo):
    _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C1", "writes": ["other/two.py"]}]}))
    run_token.mint(repo, RUN_B, "b.mjs", session_id=SELF)
    _two_rows(repo)

    out = _land(repo)

    message = _message(repo)
    assert out["committed"] is True, out
    assert "src/one.py" in _tree(repo) and "other/two.py" not in _tree(repo)
    assert [ln for ln in message.splitlines() if ln.startswith("Row-Unlanded:")] == [
        "Row-Unlanded: C2 other/two.py peer-held"
    ]
    assert "Lands-Chunks: C1" in message and "C2" not in message.split("Lands-Chunks:")[1].splitlines()[0]
    assert out["stranded"] == {"C2": ["other/two.py"]}
    assert out["incomplete_reasons"] == {"C2": "unlanded: other/two.py (peer-held)"}
    assert "other/two.py" in _git(["status", "--porcelain", "-uall"], repo)


def test_a_never_commit_row_path_is_named_with_its_reason(repo):
    _author(repo, "src/one.py", "one = 1\n")
    _author(repo, "scratch/two.txt", "two\n")
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="row one", paths=("src/one.py",)),
            ChunkCommit(id="C2", title="row two", paths=("scratch/two.txt",)),
        ),
        plan_path=None,
    )
    _write(repo, SCRIPT, "// emitted\n" + render_marker(request) + "\n")

    out = _land(repo)

    assert out["committed"] is True, out
    assert "scratch/two.txt" not in _tree(repo)
    assert "Row-Unlanded: C2 scratch/two.txt never-commit" in _message(repo)
    assert out["stranded"] == {"C2": ["scratch/two.txt"]}
