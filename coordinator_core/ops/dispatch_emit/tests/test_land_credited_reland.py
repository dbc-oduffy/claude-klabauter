"""``dispatch.land_credited`` takes no run-token claim, so an attempt that dies mid-landing cannot refuse its own re-land."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.ops.dispatch_emit import land_credited as lane
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

RUN = "warp-reland-run"
SCRIPT = "scratch/run.js"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "scratch").mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "src/a.py").write_text("v = 0\n", encoding="utf-8", newline="")
    marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="C1", title="one", paths=("src/a.py",)),)))
    (root / SCRIPT).write_text(f"// script\n{marker}\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "src/a.py").write_text("v = 1\n", encoding="utf-8", newline="")
    return root


@pytest.fixture(autouse=True)
def _drain():
    yield
    for t in list(lane.DEFERRED_RECEIPT_THREADS):
        t.join(timeout=5)


def _request() -> contract.LandCreditedRequest:
    return contract.LandCreditedRequest(run_id=RUN, plan_path="docs/plans/p.md", script_path=SCRIPT)


def _token_files(root: Path) -> list:
    base = root / ".git" / run_token._DIR
    return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()) if base.is_dir() else []


def test_attempt_killed_at_the_commit_leaves_no_claim_and_the_reland_commits(repo, monkeypatch):
    before = _token_files(repo)
    real = git_commit.commit_paths

    def dies(*args, **kwargs):
        raise TimeoutError("commit leg timed out")

    monkeypatch.setattr(git_commit, "commit_paths", dies)
    with pytest.raises(TimeoutError):
        lane.land(repo, _request())

    assert _token_files(repo) == before
    head = _git(["rev-parse", "HEAD"], repo).strip()

    monkeypatch.setattr(git_commit, "commit_paths", real)
    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["sha"] != head
    assert _token_files(repo) == before


def test_attempt_killed_before_the_commit_leaves_no_claim_and_the_reland_commits(repo, monkeypatch):
    def dies(*args, **kwargs):
        raise TimeoutError("holds leg timed out")

    monkeypatch.setattr(lane, "_holds", dies)
    with pytest.raises(TimeoutError):
        lane.land(repo, _request())
    assert _token_files(repo) == []

    monkeypatch.undo()
    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert _token_files(repo) == []


def test_a_landed_run_relanded_is_refused_as_nothing_to_land_not_as_a_claim(repo):
    first = lane.land(repo, _request())
    assert first["committed"] is True, first

    again = lane.land(repo, _request())

    assert again["committed"] is False
    assert again["refused"] == contract.REFUSED_NOTHING_TO_LAND, again
