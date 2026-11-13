"""Ratchet: the halt commit lands by named paths, and a refused one tells the commit agent to stage nothing by hand.

Replays the dependents_unstarted halt with a record and a finished row: the landing commits the record
by name, a first attempt the engine refuses (a stale run token) leaves the index untouched and its
reply carries the alternative, and no halt-path source or reply names a blanket add.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
HALT = {"halted": "dependents_unstarted", "reason": "C2 waits on C1"}
BLANKET = re.compile(r"git\s+add\s+(-A|--all|\.)(\s|$|`)|commit\s+-a\b|add\s+--all")
HALT_SOURCES = ("halted_record_commit.py", "commit_stage_halt.py", "halt_landing.py")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "README.md", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    _write(tmp_path, "reports/C1.md", "DONE: C1\n")
    _write(tmp_path, "peer_dirt.py", "peer = 1\n")
    chunk = ChunkCommit(id="C1", title="C1", paths=("pkg/c1.py",), report="reports/C1.md")
    _write(tmp_path, "run.mjs", render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None)) + "\n")
    _write(tmp_path, "pkg/c1.py", "c1 = 1\n")
    return tmp_path


def _call(root: Path, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT, **extra}
    return halted_record_commit.commit_halted_record(root, root, params)


def test_a_refused_first_attempt_names_the_alternative_and_stages_nothing(repo):
    out = _call(repo, run_token="0" * 32)

    assert out["committed"] is False and out["refused"] == "run-token", out
    assert halted_record_commit.HALT_REFUSAL_ALTERNATIVE in out["error"], out
    assert not BLANKET.search(json.dumps(out)), out
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_the_halt_commit_lands_only_named_paths_and_leaves_peer_dirt(repo):
    out = _call(repo)

    assert out.get("error") is None, out
    assert out["committed"] is True, out
    assert halted_record_commit.HALT_REFUSAL_ALTERNATIVE not in json.dumps(out)
    landed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert any(p.startswith(f"{RECORD_DIR_ROOT}/{RUN}/") for p in landed), landed
    assert all(RUN in p for p in landed), landed
    assert "peer_dirt.py" not in landed and "pkg/c1.py" not in landed
    assert "peer_dirt.py" in _git(repo, "status", "--porcelain")
    assert not BLANKET.search(_git(repo, "log", "-1", "--format=%B"))


def test_a_landed_reply_passes_through_unchanged():
    landed = {"committed": True, "sha": "a" * 40}
    assert halted_record_commit._with_named_path_alternative(dict(landed)) == landed
    held = {"committed": False, "deferred": "session-landing-hold"}
    assert halted_record_commit._with_named_path_alternative(dict(held)) == held


def test_a_refusal_with_no_error_text_carries_the_alternative_as_remedy():
    out = halted_record_commit._with_named_path_alternative({"committed": False, "refused": "x"})
    assert out["remedy"] == halted_record_commit.HALT_REFUSAL_ALTERNATIVE


def test_no_halt_path_source_names_a_blanket_add():
    here = Path(halted_record_commit.__file__).parent
    for name in HALT_SOURCES:
        assert not BLANKET.search((here / name).read_text(encoding="utf-8")), name
