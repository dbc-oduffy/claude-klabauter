"""
Pins the three archive-family sites the L18 audit rated needs-test
(state/audits/2026-10-condition-evaluated-once-sweep.md): each evaluates a
destination or cap condition for the whole batch before the per-item moves
apply. Real on-disk git throughout; the tests read repo state, never mocks.

Each defect the audit's reading suspected and these runs reproduced is pinned
here as the correct behaviour.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import archive_paper_trail as paper_trail
from coordinator_core.ops.fleet import archive_plans as plans
from coordinator_core.ops.fleet import archive_queue_entry as queue_entry
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> None:
    # popup-intentional-last-resort — test-only real-git spawn: the tracked/untracked
    # distinction these sites depend on cannot be mocked honestly.
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


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")


def _files(root: Path, sub: str) -> set:
    return {p.relative_to(root).as_posix() for p in (root / sub).rglob("*") if p.is_file()}


def _queue_repo(tmp_path: Path, names: list) -> "tuple[Path, Path]":
    root = _repo(tmp_path)
    queue_dir = root / "state" / "improvement-queue"
    for name in names:
        path = queue_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("status: closed\n", encoding="utf-8")
    _commit_all(root)
    return root, queue_dir


_ENTRY = "state/improvement-queue/2026-09-01-x.yaml"


def test_queue_entry_repeated_entry_path_reports_the_archive_that_happened(tmp_path: Path) -> None:
    root, queue_dir = _queue_repo(tmp_path, ["2026-09-01-x.yaml"])

    result = asyncio.run(queue_entry._handle_batch(root, queue_dir, [_ENTRY, _ENTRY], False))

    assert "archive/improvement-queue/2026-09/2026-09-01-x.yaml" in _files(root, "archive")
    assert any(item["archived"] for item in result["items"])
    assert result["exit_code"] == 0


def test_queue_entry_same_basename_in_two_subdirs_archives_one_and_names_the_other(
    tmp_path: Path,
) -> None:
    root, queue_dir = _queue_repo(
        tmp_path, ["s1/2026-09-01-y.yaml", "s2/2026-09-01-y.yaml"]
    )
    first = "state/improvement-queue/s1/2026-09-01-y.yaml"
    second = "state/improvement-queue/s2/2026-09-01-y.yaml"

    result = asyncio.run(queue_entry._handle_batch(root, queue_dir, [first, second], False))

    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[first]["archived"] is True and by_id[first]["error"] is None
    assert by_id[second]["archived"] is False and by_id[second]["error"]
    assert (root / second).is_file()
    assert result["exit_code"] == 1


def _paper_trail_partial_failure(tmp_path: Path) -> "tuple[Path, dict]":
    root = _repo(tmp_path)
    workdir = root / "docs" / "research" / "run1-workdir"
    workdir.mkdir(parents=True)
    (workdir / "a.md").write_text("a\n", encoding="utf-8")
    (workdir / "b.md").write_text("b\n", encoding="utf-8")
    _commit_all(root)
    (workdir / "untracked.md").write_text("never committed\n", encoding="utf-8")
    params = {
        "repo_root": str(root / ".git"),
        "run_id": "run1",
        "topic_slug": "t",
        "dry_run": False,
    }
    return root, params


def test_paper_trail_partial_failure_moves_the_tracked_files_and_reports_the_rest(
    tmp_path: Path,
) -> None:
    root, params = _paper_trail_partial_failure(tmp_path)

    result = asyncio.run(paper_trail._handler(params, root / ".git"))

    assert result["archived"] is False
    assert [item["id"] for item in result["failed"]] == [
        "docs/research/run1-workdir/untracked.md"
    ]
    assert _files(root, "docs/research/run1-workdir") == {
        "docs/research/run1-workdir/untracked.md"
    }
    assert len(_files(root, "docs/research/archive")) == 2


def test_paper_trail_rerun_after_partial_failure_names_what_is_stranded(tmp_path: Path) -> None:
    root, params = _paper_trail_partial_failure(tmp_path)
    asyncio.run(paper_trail._handler(params, root / ".git"))

    rerun = asyncio.run(paper_trail._handler(params, root / ".git"))

    assert rerun.get("failed") or rerun.get("error")


def _plan_with_sidecar(root: Path, stem: str) -> None:
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for name in (f"{stem}.md", f"{stem}.a.md"):
        (plans_dir / name).write_text(
            '---\ntitle: "x"\nstatus: implemented\n---\n\nbody\n', encoding="utf-8"
        )


def test_plan_cap_never_splits_a_primary_from_its_sidecar(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _plan_with_sidecar(root, "2026-08-25-capped")
    _commit_all(root)

    moves, skipped = plans.plan_sweep(root, root, cap=1)

    planned = {m.candidate_id for m in moves}
    deferred = {s["id"] for s in skipped}
    primary, sidecar = "docs/plans/2026-08-25-capped.md", "docs/plans/2026-08-25-capped.a.md"
    assert (primary in planned) == (sidecar in planned)
    assert (primary in deferred) == (sidecar in deferred)
