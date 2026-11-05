"""fleet.archive_paper_trail lands the workdir in its run dir, never under docs/research/archive."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import date
from pathlib import Path

import pytest

from coordinator_core.ops import _research_layout as layout
from coordinator_core.ops.fleet import archive_paper_trail as paper_trail
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> None:
    # popup-intentional-last-resort — test-only real-git spawn: git mv needs a real index.
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    return root


def _seed_workdir(root: Path) -> None:
    workdir = root / "docs" / "research" / "run1-workdir"
    (workdir / "seg").mkdir(parents=True)
    (workdir / "a.md").write_text("a\n", encoding="utf-8")
    (workdir / "seg" / "b.md").write_text("b\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")


def _params(root: Path, **extra) -> dict:
    return {
        "repo_root": str(root / ".git"),
        "run_id": "run1",
        "topic_slug": "pathing-survey",
        "dry_run": False,
        **extra,
    }


def _files(root: Path, sub: str) -> set:
    return {p.relative_to(root).as_posix() for p in (root / sub).rglob("*") if p.is_file()}


def _run(root: Path, params: dict) -> dict:
    return asyncio.run(paper_trail._handler(params, root / ".git"))


def test_workdir_lands_in_the_run_dir_and_classifies_run_child(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _seed_workdir(root)
    today = date.today().isoformat()
    run = f"docs/research/pathing/{today}-pathing-survey"

    result = _run(root, _params(root))

    assert result["archived"] is True
    assert result["dest"] == f"{run}/workdir"
    landed = _files(root, run)
    assert landed == {f"{run}/workdir/a.md", f"{run}/workdir/seg/b.md"}
    assert all(layout.classify(p.removeprefix("docs/research/")) == layout.RUN_CHILD for p in landed)
    assert not (root / "docs" / "research" / "archive").exists()
    assert not (root / "docs" / "research" / "run1-workdir").exists()


def test_explicit_topic_names_the_topic_dir(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _seed_workdir(root)
    today = date.today().isoformat()

    result = _run(root, _params(root, topic="nav"))

    assert result["dest"] == f"docs/research/nav/{today}-pathing-survey/workdir"


def test_unfoldable_slug_files_under_misc(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _seed_workdir(root)
    today = date.today().isoformat()

    result = _run(root, _params(root, topic_slug="2026-01-01-odd"))

    assert result["dest"] == f"docs/research/misc/{today}-2026-01-01-odd/workdir"


def test_dry_run_reports_the_run_dir_dest_and_mutates_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _seed_workdir(root)

    result = _run(root, _params(root, dry_run=True))

    assert result["archived"] is False and result["already_archived"] is False
    assert result["dest"].endswith("-pathing-survey/workdir")
    assert (root / "docs" / "research" / "run1-workdir" / "a.md").is_file()
    assert not (root / "docs" / "research" / "pathing").exists()


def test_legacy_archive_dir_still_reads_as_already_archived(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    legacy = root / "docs" / "research" / "archive" / "2026-01-01-pathing-survey"
    legacy.mkdir(parents=True)
    (legacy / "a.md").write_text("a\n", encoding="utf-8")

    result = _run(root, _params(root))

    assert result == {
        "archived": False,
        "dest": "docs/research/archive/2026-01-01-pathing-survey",
        "already_archived": True,
    }


def test_run_dir_workdir_from_an_earlier_day_reads_as_already_archived(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    earlier = root / "docs" / "research" / "pathing" / "2026-01-01-pathing-survey" / "workdir"
    earlier.mkdir(parents=True)
    (earlier / "a.md").write_text("a\n", encoding="utf-8")

    result = _run(root, _params(root))

    assert result["already_archived"] is True and result["archived"] is False
    assert result["dest"] == "docs/research/pathing/2026-01-01-pathing-survey/workdir"


def test_archive_reserved_topic_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _seed_workdir(root)

    with pytest.raises(ValueError):
        _run(root, _params(root, topic="archive"))

    assert (root / "docs" / "research" / "run1-workdir" / "a.md").is_file()
    assert not (root / "docs" / "research" / "archive").exists()
