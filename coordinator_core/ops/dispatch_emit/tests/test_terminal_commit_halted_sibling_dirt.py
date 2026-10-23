"""A halted, unresumed sibling run's declared writes are never swept into another run's terminal
commit; the commit names the dirty ones under ``held_by_halted_run``. A resumed marker and an
out-of-window run carry the same files, so the test discriminates."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_companion
from coordinator_core.ops.dispatch_emit import run_token, stopped_runs, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a3"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
JOB = "tools/runpod-jobs/job.py"
PROTO = "tools/likeness-prototype/x.py"
PROTO_PREFIX = "tools/likeness-prototype"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _run_id(age_seconds: float = 60, suffix: str = "aaaaaa") -> str:
    stamp = datetime.fromtimestamp(time.time() - age_seconds, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"ask-{stamp}-{suffix}"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _halted_run(root: Path, run_id: str, *, status: str = "halted", writes=(JOB,),
                prefixes=(PROTO_PREFIX,), doc_writes=()) -> None:
    _write(root, f"state/warp/{run_id}/halted.json", json.dumps({"status": status}))
    _write(root, f"state/warp/{run_id}/manifest.json",
           json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": list(doc_writes)}))
    if prefixes:
        request = CommitRequest(chunks=(ChunkCommit(id="C1", title="h", paths=(), prefixes=tuple(prefixes)),))
        _write(root, f"scratch/warp/{run_id}/commit-request.txt", render_marker(request) + "\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _write(root, "tools/clean.py", "clean\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    _write(root, "src/a.py", "a\n")
    _write(root, JOB, "job\n")
    _write(root, PROTO, "proto\n")
    for rel in ("src/a.py", JOB, PROTO, "tools/clean.py"):
        session_scope.touch(SELF, rel, cwd=str(root))
    return root


def _run_r(repo: Path) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),))
    (repo / "run-r.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run-r.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    """Paths the last terminal commit landed: HEAD plus the product commit a follow-on HEAD
    (`Own-Left-Behind-Of:` trailer) is attributed to."""
    import re

    def files(rev: str) -> set:
        out = _git(["show", "--numstat", "--format=", rev], repo)
        return {ln.split("\t")[-1] for ln in out.splitlines() if ln.strip()}

    landed = files("HEAD")
    product = re.search(r"Own-Left-Behind-Of: ([0-9a-f]{40})", _git(["log", "-1", "--format=%B", "HEAD"], repo))
    if product:
        landed |= files(product.group(1))
    return sorted(landed)


def test_a_halted_siblings_declared_writes_are_held_dirty_and_named(repo):
    halted = _run_id()
    _halted_run(repo, halted)
    out = _run_r(repo)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/a.py"]
    status = _git(["status", "--porcelain", "--untracked-files=all"], repo)
    assert JOB in status and PROTO in status
    assert out["held_by_halted_run"] == {JOB: [halted], PROTO: [halted]}


def test_control_a_resumed_marker_no_longer_holds_its_paths(repo):
    _halted_run(repo, _run_id(), status="resumed")
    out = _run_r(repo)
    assert out["committed"] is True, out
    assert sorted(_committed(repo)) == sorted([JOB, PROTO, "src/a.py"])
    assert "held_by_halted_run" not in out


def test_control_an_out_of_window_halted_run_no_longer_holds_its_paths(repo):
    _halted_run(repo, _run_id(age_seconds=run_token.TTL_SECONDS + 3600))
    out = _run_r(repo)
    assert out["committed"] is True, out
    assert sorted(_committed(repo)) == sorted([JOB, PROTO, "src/a.py"])
    assert "held_by_halted_run" not in out


def test_a_clean_path_the_halted_run_declares_is_not_reported(repo):
    halted = _run_id()
    _halted_run(repo, halted, writes=(JOB, "tools/clean.py"))
    out = _run_r(repo)
    assert out["committed"] is True, out
    assert out["held_by_halted_run"] == {JOB: [halted], PROTO: [halted]}
    assert "tools/clean.py" not in out["held_by_halted_run"]


def test_the_runs_own_halted_record_is_not_a_hold_against_itself(repo):
    own = "run-r"
    assert terminal_commit._live_run_claims(repo, [JOB], own) == set()
    _halted_run(repo, _run_id())
    held: dict = {}
    assert terminal_commit._live_run_claims(repo, [JOB, "src/a.py"], own, held) == {JOB}
    assert list(held) == [JOB]


def _stopped_run_residue(root: Path, tmp_path: Path, monkeypatch, status: str):
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit.tests import test_stopped_runs as ts

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    monkeypatch.setattr(session_scope, "contested_by_live_peers", lambda paths, sid, cwd=None: {})
    stopped = ts._run_id(60, "bbbbbb")
    ts._submit(root, tmp_path, stopped, body=ts._stopped_body(stopped))
    ts._artifacts(root, stopped, sizing="state/sizings/a.yaml")
    halted = _run_id(30, "cccccc")
    _halted_run(root, halted, status=status, writes=(), prefixes=(), doc_writes=("state/sizings/a.yaml",))
    with commit_companion.excluding_sibling_claims(
        lambda candidates: terminal_commit._live_run_claims(root, candidates, stopped)
    ):
        return stopped, stopped_runs.residue(root, ts.SESSION, [])


def test_stopped_run_residue_does_not_carry_a_halted_runs_declared_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    stopped, got = _stopped_run_residue(root, tmp_path, monkeypatch, "halted")
    assert "state/sizings/a.yaml" not in got.carried.get(stopped, ())
    assert f"state/warp/{stopped}/requirements.md" in got.carried[stopped]


def test_control_stopped_run_residue_carries_the_path_once_the_halted_run_resumes(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    stopped, got = _stopped_run_residue(root, tmp_path, monkeypatch, "resumed")
    assert "state/sizings/a.yaml" in got.carried[stopped]
