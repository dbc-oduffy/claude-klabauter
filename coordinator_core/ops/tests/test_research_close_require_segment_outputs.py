"""research.close ``require_segment_outputs``: every fired segment's synthesis must land or the close refuses."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIZING = "state/sizings/s.yaml"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "state/sizings").mkdir(parents=True)
    _git(r, "init", "-q")
    (r / _SIZING).write_text("id: s\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    s = tmp_path / "scratch"
    (s / "repo").mkdir(parents=True)
    (s / "synthesis.md").write_text("web\n")
    (s / "repo" / "synthesis.md").write_text("repo\n")
    return s


def _close(repo: Path, scratch: Path, **extra) -> dict:
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": "r1", "topic_slug": "topic",
        "segment_dirs": ["repo"], "require_segment_outputs": ["synthesis.md", "repo/synthesis.md"],
    }
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def test_example_game_repo_shape_missing_root_synthesis_is_refused(repo, scratch):
    (scratch / "synthesis.md").unlink()
    head = _git(repo, "rev-parse", "HEAD")
    before = (repo / _SIZING).read_bytes()
    out = _close(repo, scratch, sizing=_SIZING)
    assert out["exit_code"] == 1 and out["committed"] is False and out["deliverable"] is False, out
    assert "synthesis.md is missing or empty" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and not (repo / "docs").exists()
    assert (repo / _SIZING).read_bytes() == before


def test_root_entry_is_refused_even_when_listed_in_halted(repo, scratch):
    (scratch / "synthesis.md").unlink()
    out = _close(repo, scratch, halted_segments=["repo", "synthesis.md"])
    assert out["exit_code"] == 1 and out["deliverable"] is False, out


def test_nested_entry_of_halted_segment_is_excused(repo, scratch):
    (scratch / "repo" / "synthesis.md").unlink()
    out = _close(repo, scratch, halted_segments=["repo"])
    assert out["exit_code"] == 0 and out["committed"] is True, out


def test_nested_entry_of_unhalted_segment_is_refused(repo, scratch):
    (scratch / "repo" / "synthesis.md").unlink()
    out = _close(repo, scratch)
    assert out["exit_code"] == 1 and out["deliverable"] is False, out


def test_halted_segment_outside_segment_dirs_does_not_excuse(repo, scratch):
    (scratch / "repo" / "synthesis.md").unlink()
    out = _close(repo, scratch, halted_segments=["other"])
    assert out["exit_code"] == 1 and out["deliverable"] is False, out


def test_empty_file_is_refused(repo, scratch):
    (scratch / "repo" / "synthesis.md").write_text("")
    out = _close(repo, scratch)
    assert out["exit_code"] == 1 and out["deliverable"] is False, out


@pytest.mark.parametrize("bad", [["../x"], "synthesis.md", [""], [3]])
def test_escaping_entry_and_non_list_are_refused(repo, scratch, bad):
    head = _git(repo, "rev-parse", "HEAD")
    out = _close(repo, scratch, require_segment_outputs=bad)
    assert out["exit_code"] == 1 and out["committed"] is False, out
    assert "require_segment_outputs" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_all_present_closes(repo, scratch):
    out = _close(repo, scratch)
    assert out["exit_code"] == 0 and out["committed"] is True, out
