"""End to end: research.close then research.archive_workdir at default params land one run dir.

Drives both production handlers in a tmp git repo seeded with a legacy flat research dir and a
legacy trail record; every new path must classify run-child and the legacy entries must still read.
"""

from __future__ import annotations

import asyncio
import datetime
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import _research_layout as layout
from coordinator_core.ops import _research_trail
from coordinator_core.ops.research_archive_workdir import _handler as archive_handler
from coordinator_core.ops.research_close import _handler as close_handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SLUG = "market-survey"
_TOPIC = "market"
_LEGACY_FLAT = "docs/research/2026-01-01-old-topic"
_LEGACY_TRAIL = "docs/research/trail/old-run.yaml"


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
    flat = r / _LEGACY_FLAT
    flat.mkdir(parents=True)
    (flat / "digest.md").write_text("legacy digest\n")
    legacy_trail = r / _LEGACY_TRAIL
    legacy_trail.parent.mkdir(parents=True)
    legacy_trail.write_text('run_id: "old-run"\ntier: "scouts"\n')
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    s = tmp_path / "scratch"
    (s / "segments").mkdir(parents=True)
    (s / "synthesis.md").write_text("synthesis body\n")
    (s / "verdict.md").write_text("verdict body\n")
    (s / "segments" / "seg-1.md").write_text("segment one\n")
    return s


def _top(repo: Path) -> set[str]:
    return {p.name for p in (repo / "docs" / "research").iterdir()}


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_close_then_archive_workdir_land_one_run_dir_and_legacy_still_reads(repo, scratch):
    top_before = _top(repo)
    today = datetime.date.today().isoformat()
    run_rel = f"docs/research/{_TOPIC}/{today}-{_SLUG}"

    out = asyncio.run(close_handler(
        {"scratch_dir": str(scratch), "tier": "corpus", "run_id": "r1", "topic_slug": _SLUG,
         "outputs": ["synthesis.md", "verdict.md", "segments/seg-1.md"]},
        repo / ".git",
    ))
    assert out["exit_code"] == 0 and out["committed"] is True, out
    assert out["run_dir"] == run_rel and out["workdir"] == f"{run_rel}/workdir"

    committed = set(_git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
    assert committed >= {f"{run_rel}/synthesis.md", f"{run_rel}/verdict.md", f"{run_rel}/trail.yaml"}
    assert any(p.startswith(f"{run_rel}/outputs/") for p in committed)

    workdir = repo / "docs" / "research" / f"r1-{_SLUG}-workdir"
    (workdir / "notes").mkdir(parents=True)
    (workdir / "notes" / "scratch.txt").write_text("scratch\n")
    arch = asyncio.run(archive_handler({"run_id": "r1"}, repo / ".git"))
    assert arch["exit_code"] == 0 and arch["archived"] is True, arch
    assert Path(arch["dest"]) == repo / run_rel / "workdir"
    assert (repo / run_rel / "workdir" / "notes" / "scratch.txt").read_text() == "scratch\n"
    assert not workdir.exists()

    research = repo / "docs" / "research"
    new_files = [p for p in _files_under(research / _TOPIC)]
    assert new_files
    for p in new_files:
        rel = p.relative_to(research).as_posix()
        assert layout.classify(rel) == layout.RUN_CHILD, rel
    assert layout.classify(f"{_TOPIC}/{today}-{_SLUG}") == layout.RUN_DIR

    assert _top(repo) - top_before == {_TOPIC}
    assert not (research / "archive").exists()
    assert not any(n.endswith("-workdir") for n in _top(repo))
    assert (repo / run_rel / "trail.yaml").is_file()

    assert _research_trail.locate_trail(repo, "old-run") == _LEGACY_TRAIL
    assert _research_trail.locate_trail(repo, "r1") == f"{run_rel}/trail.yaml"
    assert (repo / _LEGACY_FLAT / "digest.md").read_text() == "legacy digest\n"
    assert not (research / "trail" / "r1.yaml").exists()


def test_a_stray_outside_the_run_dir_is_noted_not_refused_and_left_intact(repo, scratch):
    stray = repo / "docs" / "research" / "2026-11-03-stray-dump"
    stray.mkdir()
    (stray / "late.md").write_text("late\n")
    out = asyncio.run(close_handler(
        {"scratch_dir": str(scratch), "tier": "corpus", "run_id": "r1", "topic_slug": _SLUG,
         "outputs": ["synthesis.md"]},
        repo / ".git",
    ))
    assert out["exit_code"] == 0 and out["committed"] is True and out["deliverable"] is True, out
    assert out["peer_files"] == ["docs/research/2026-11-03-stray-dump/late.md"]
    assert (stray / "late.md").read_text() == "late\n"
    assert "late.md" not in _git(repo, "show", "--name-only", "--format=", "HEAD")
