"""research.close group_runs: the research block drops only on the last sibling group's close."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIZING = "state/sizings/x.yaml"
_TEXT = "size: S\nresearch:\n  class: corpus\nnote: keep\n"
_RUNS = ["ra", "rb", "rc"]


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


def _close(repo: Path, tmp_path: Path, run_id: str, **extra) -> dict:
    scratch = tmp_path / f"scratch-{run_id}"
    scratch.mkdir(exist_ok=True)
    (scratch / "final.md").write_text(f"body {run_id}\n")
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": run_id,
        "topic_slug": f"topic-{run_id}", "outputs": ["final.md"], "sizing": _SIZING,
        "group_runs": list(_RUNS),
    }
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def _has_block(repo: Path) -> bool:
    return "research:" in (repo / _SIZING).read_text()


def test_the_block_is_kept_while_a_sibling_is_open(repo, tmp_path):
    out = _close(repo, tmp_path, "ra")
    assert out["exit_code"] == 0 and out["research_block_dropped"] is False, out
    assert out["research_block"] == "kept (1 of 3 groups closed)"
    assert _has_block(repo)
    assert _git(repo, "status", "--porcelain") == ""


def test_the_last_group_drops_the_block_in_its_close_commit(repo, tmp_path):
    _close(repo, tmp_path, "ra")
    mid = _close(repo, tmp_path, "rb")
    assert mid["research_block"] == "kept (2 of 3 groups closed)" and _has_block(repo)
    last = _close(repo, tmp_path, "rc")
    assert last["exit_code"] == 0 and last["research_block_dropped"] is True, last
    assert "research_block" not in last
    assert not _has_block(repo) and "note: keep" in (repo / _SIZING).read_text()
    changed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert _SIZING in changed
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("order", [["ra", "rb"], ["rb", "ra"]])
def test_two_sibling_closes_in_either_order_drop_exactly_once(repo, tmp_path, order):
    runs = ["ra", "rb"]
    first = _close(repo, tmp_path, order[0], group_runs=runs)
    second = _close(repo, tmp_path, order[1], group_runs=runs)
    assert first["research_block_dropped"] is False
    assert second["research_block_dropped"] is True
    assert not _has_block(repo)
    again = _close(repo, tmp_path, order[0], group_runs=runs)
    assert again["exit_code"] == 0 and again["research_block_dropped"] is False
    assert _git(repo, "status", "--porcelain") == ""


def test_a_run_id_outside_group_runs_is_refused_with_no_commit(repo, tmp_path):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, tmp_path, "ra", group_runs=["rb", "rc"])
    assert out["exit_code"] == 1 and "must include this run_id" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


@pytest.mark.parametrize("bad", [["ra", "../x"], ["ra", ""], "ra", [], ["ra", 3]])
def test_an_unsafe_group_runs_is_refused_before_any_copy(repo, tmp_path, bad):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, tmp_path, "ra", group_runs=bad)
    assert out["exit_code"] == 1 and "group_runs" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()
    assert _has_block(repo)


def test_a_refused_commit_on_the_last_group_restores_the_block(repo, tmp_path, monkeypatch):
    from coordinator_core.git.commit import CommitRefused
    from coordinator_core.ops import research_close

    _close(repo, tmp_path, "ra", group_runs=["ra", "rb"])

    def refuse(*_a, **_k):
        raise CommitRefused("no")

    monkeypatch.setattr(research_close, "commit_paths", refuse)
    out = _close(repo, tmp_path, "rb", group_runs=["ra", "rb"])
    assert out["exit_code"] == 1 and "commit refused" in out["error"]
    assert _has_block(repo)
