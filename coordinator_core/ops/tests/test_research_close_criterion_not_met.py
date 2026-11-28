"""research.close halts criterion_not_met while the sizing's register still owes rows."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.research_close import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_REL = "state/sizings/x.yaml"
_HEAD = "size: S\nresearch:\n  class: corpus\nnote: keep\n"
_OPEN = "requirement_register:\n  rows:\n  - id: T18\n    surface: s\n    status: open\n"
_SETTLED = "requirement_register:\n  rows:\n  - id: T18\n    surface: s\n    status: met\n    wired: true\n"


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
    (r / ".git" / "info" / "exclude").write_text(".coordinator-local/\n")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    s = tmp_path / "scratch"
    s.mkdir()
    (s / "synthesis.md").write_text("synthesis body\n")
    return s


def _sizing(repo: Path, register: str) -> Path:
    p = repo / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_HEAD + register, encoding="utf-8")
    return p


def _close(repo: Path, scratch: Path) -> dict:
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": "r1", "topic_slug": "topic",
        "outputs": ["synthesis.md"], "sizing": _REL,
    }
    return asyncio.run(_handler(params, repo / ".git"))


def test_open_register_rows_halt_the_close_keep_the_block_and_commit_the_outputs(repo, scratch):
    p = _sizing(repo, _OPEN)
    before = p.read_bytes()
    out = _close(repo, scratch)
    assert out["exit_code"] == 1 and out["halted"] == "criterion_not_met", out
    assert out["committed"] is True and out["deliverable"] is False
    assert out["research_block_dropped"] is False
    assert [r["id"] for r in out["open_rows"]] == ["T18"]
    assert out["next_action"]["op"] == "engine-ask refire" and _REL in out["next_action"]["fire"]
    assert p.read_bytes() == before and "research:" in p.read_text()
    assert out["paths"] and _git(repo, "ls-files", out["trail"]) == out["trail"]
    assert _git(repo, "show", f"HEAD:{out['paths'][0]}") == "synthesis body"


def test_settled_register_still_closes_green_and_drops_the_block(repo, scratch):
    p = _sizing(repo, _SETTLED)
    out = _close(repo, scratch)
    assert out["exit_code"] == 0 and out["research_block_dropped"] is True, out
    assert "halted" not in out and "open_rows" not in out
    assert "research:" not in p.read_text()


def test_a_reclose_after_the_halt_stays_halted_and_adds_no_commit(repo, scratch):
    _sizing(repo, _OPEN)
    first = _close(repo, scratch)
    before = int(_git(repo, "rev-list", "--count", "HEAD"))
    again = _close(repo, scratch)
    assert again["halted"] == "criterion_not_met" and again["exit_code"] == 1, again
    assert again["sha"] == first["sha"] and int(_git(repo, "rev-list", "--count", "HEAD")) == before
