"""A won run-token redeem drops the run's staged twins at the run_token seam; a refused one leaves them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
RUN = "plan-20261021T100000-aaaaaa"
OTHER = "plan-20261021T100000-bbbbbb"
SCRIPT = "run-a.mjs"
SHA = "a" * 64


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _twin(root: Path, run: str = RUN) -> Path:
    return root / "scratch" / "warp" / f"{run}.staged-{SHA}.workflow.mjs"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),), plan_path=None)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    _write(root, "scratch/warp/" + _twin(root).name, "twin")
    _write(root, "scratch/warp/" + _twin(root, OTHER).name, "peer twin")
    return root


def _land(root: Path, **extra) -> dict:
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "session_id": SELF, "run_id": RUN,
        "inline_review": {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}, **extra,
    }
    return land_run._handler(params, repo_root=root / ".git")


def test_land_run_redeem_drops_this_runs_twin_only(repo):
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)
    _land(repo, run_token=token)
    assert not _twin(repo).exists()
    assert _twin(repo, OTHER).exists()


def test_land_run_refused_redeem_leaves_the_twin(repo):
    run_token.mint(repo, RUN, SCRIPT, session_id=SELF)
    out = _land(repo, run_token="0" * 32)
    assert out.get("refused") == "run-token", out
    assert _twin(repo).exists()


def test_redeem_itself_is_the_seam(repo):
    token = run_token.mint(repo, RUN, SCRIPT)
    assert run_token.redeem(repo, RUN, "0" * 32, SCRIPT) is False
    assert _twin(repo).exists()
    assert run_token.redeem(repo, RUN, token, SCRIPT) is True
    assert not _twin(repo).exists()
    assert _twin(repo, OTHER).exists()
