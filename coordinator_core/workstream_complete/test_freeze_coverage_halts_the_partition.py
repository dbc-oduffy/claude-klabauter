
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete.directives_review import (
    ReviewSlice,
    build_review_partition_freeze_directives,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    )


def _init_repo(root: Path) -> None:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "test"], root)


def _commit(root: Path, name: str, content: str, message: str) -> str:
    (root / name).write_text(content, encoding="utf-8")
    _git(["add", name], root)
    _git(["commit", "-q", "-m", message], root)
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def test_freeze_coverage_halts_the_partition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "a v1\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "a v2\n", "modify a.txt")
    range_ = f"{sha1}..{sha2}"

    monkeypatch.chdir(tmp_path)

    slices = [
        ReviewSlice(slice_id="covered", paths=("a.txt",)),
        ReviewSlice(slice_id="narrowed", paths=("a.txt", "no-such-change.txt")),
    ]
    directives = build_review_partition_freeze_directives(range_, slices)

    exit_code, report = ws_apply._execute_directives(directives, [], {})

    assert report["blocked"] == []
    assert "d-freeze-and-dispatch-review-partition-covered" in report["landed"]
    failed_ids = [entry["id"] for entry in report["failed"]]
    assert failed_ids == ["d-freeze-and-dispatch-review-partition-narrowed"]
    assert "d-freeze-and-dispatch-review-partition-narrowed" not in report["landed"]
