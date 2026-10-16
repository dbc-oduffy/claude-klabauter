"""research.close ``require_outputs``: the named deliverable must land or the close refuses."""

from __future__ import annotations

import asyncio
import datetime
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SEGS = ["repo-1-a", "repo-compare"]


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
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    s = tmp_path / "scratch"
    (s / "repo-1-a").mkdir(parents=True)
    (s / "repo-compare").mkdir()
    (s / "final.md").write_text("final\n")
    (s / "repo-1-a" / "synthesis.md").write_text("syn\n")
    (s / "repo-compare" / "comparison.md").write_text("ranked\n")
    return s


def _close(repo: Path, scratch: Path, **extra) -> dict:
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": "r1", "topic_slug": "topic",
        "segment_dirs": _SEGS, "require_outputs": ["repo-compare/comparison.md"],
    }
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def test_present_comparison_closes_and_is_archived(repo, scratch):
    out = _close(repo, scratch)
    assert out["exit_code"] == 0 and out["committed"] is True, out
    dest = f"docs/research/{datetime.date.today().isoformat()}-topic/repo-compare/comparison.md"
    assert dest in out["paths"]


@pytest.mark.parametrize("content", [None, ""])
def test_missing_or_empty_comparison_refuses_without_commit(repo, scratch, content):
    target = scratch / "repo-compare" / "comparison.md"
    if content is None:
        target.unlink()
    else:
        target.write_text(content)
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch)
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False, out
    assert "repo-compare/comparison.md is missing or empty" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()


def test_halted_segment_does_not_excuse_a_required_output(repo, scratch):
    (scratch / "repo-compare" / "comparison.md").unlink()
    out = _close(repo, scratch, halted_segments=["repo-compare"])
    assert out["exit_code"] == 1 and out["deliverable"] is False, out
    assert out["halted_segments"] == ["repo-compare"]


@pytest.mark.parametrize("bad", [["../x"], "repo-compare/comparison.md", [""], [3]])
def test_escaping_entry_and_non_list_are_refused(repo, scratch, bad):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, require_outputs=bad)
    assert out["exit_code"] == 1 and out["committed"] is False, out
    assert "require_outputs" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
