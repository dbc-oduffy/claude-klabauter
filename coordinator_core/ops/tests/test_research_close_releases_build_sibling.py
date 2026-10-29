"""research.close frees the build run held ``after`` the research run once the research block drops."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIZING = "state/sizings/x.yaml"
_TEXT = "size: S\nresearch:\n  class: corpus\nnote: keep\n"
_PARENT = "ask-p1"
_RESEARCH = "rr"
_BUILD = "rb"
_BUILD_SIZING = "state/sizings/build-slice.yaml"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "seed.txt").write_text("seed\n")
    p = r / _SIZING
    p.parent.mkdir(parents=True)
    p.write_bytes(_TEXT.encode("utf-8"))
    _git(r, "add", "seed.txt", _SIZING)
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _split(repo: Path, research_status: str = contract.STATUS_RUNNING) -> None:
    parent = contract.ParentRecord(
        parent_id=_PARENT,
        runs=(
            contract.SiblingRun(_RESEARCH, "research", (), "state/sizings/research-slice.yaml", research_status),
            contract.SiblingRun(_BUILD, "build", (_RESEARCH,), _BUILD_SIZING, contract.STATUS_AFTER),
        ),
    )
    for rel, text in (
        (contract.parent_record_rel(_PARENT), json.dumps(parent.to_json())),
        (contract.status_rel(_RESEARCH), json.dumps({"status": research_status, "parent_id": _PARENT})),
        (contract.status_rel(_BUILD), json.dumps({"status": contract.STATUS_AFTER, "parent_id": _PARENT})),
    ):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def _close(repo: Path, tmp_path: Path, run_id: str = _RESEARCH, **extra) -> dict:
    scratch = tmp_path / f"scratch-{run_id}"
    scratch.mkdir(exist_ok=True)
    (scratch / "final.md").write_text(f"body {run_id}\n")
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": run_id,
        "topic_slug": f"topic-{run_id}", "outputs": ["final.md"], "sizing": _SIZING,
    }
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def test_close_with_sizing_releases_the_build_run(repo, tmp_path):
    _split(repo)
    out = _close(repo, tmp_path)
    assert out["exit_code"] == 0 and out["research_block_dropped"] is True, out
    assert out["released_runs"] == [_BUILD]
    status = json.loads((repo / contract.status_rel(_RESEARCH)).read_text())
    assert status["status"] == contract.STATUS_HALTED and status[contract.CLOSED_BY_KEY].endswith(f"{_RESEARCH}.yaml")


def test_unsplit_research_run_releases_nothing(repo, tmp_path):
    out = _close(repo, tmp_path)
    assert out["exit_code"] == 0 and out["research_block_dropped"] is True, out
    assert "released_runs" not in out


def test_refused_close_releases_nothing(repo, tmp_path):
    _split(repo)
    out = _close(repo, tmp_path, outputs=["missing.md"])
    assert out["exit_code"] == 1, out
    assert "released_runs" not in out
    status = json.loads((repo / contract.status_rel(_RESEARCH)).read_text())
    assert status["status"] == contract.STATUS_RUNNING


def test_partial_group_close_releases_nothing(repo, tmp_path):
    _split(repo)
    out = _close(repo, tmp_path, group_runs=[_RESEARCH, "other"])
    assert out["research_block_dropped"] is False and out["research_block"].startswith("kept"), out
    assert "released_runs" not in out
    status = json.loads((repo / contract.status_rel(_RESEARCH)).read_text())
    assert status["status"] == contract.STATUS_RUNNING


def test_close_without_sizing_releases_nothing(repo, tmp_path):
    _split(repo)
    out = _close(repo, tmp_path, sizing=None)
    assert out["exit_code"] == 0 and out["research_block_dropped"] is False, out
    assert "released_runs" not in out
