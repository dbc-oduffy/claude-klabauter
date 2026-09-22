"""
coordinator_core.workstream_complete.test_freeze_coverage_halts_the_partition
— AC-P1-5: the real `freeze-review-diff.py` CLI, dispatched through
`workstream_complete.apply._execute_directives` exactly as
`d-freeze-and-dispatch-review-partition-<slice-id>` directives are dispatched
in production, refuses (exit 4) a slice whose `--paths` list names an entry
with no change in the range — landing that slice in `report["failed"]`
rather than `report["landed"]`, and never blocking a sibling slice whose
`--paths` are fully covered.

Purpose: `test_apply.py`'s own `_execute_directives` seam
(`coordinator_core/workstream_complete/test_apply.py`) stubs every CLI via
`monkeypatch.setattr(ws_apply, "_load_cli_module", ...)` — a fake module
standing in for the real one. That seam cannot prove AC-P1-5: the point
under test IS `freeze-review-diff.py`'s own real exit code (4 on an
uncovered `--paths` entry, 0 otherwise), so this test leaves
`ws_apply._load_cli_module` unpatched and lets `_execute_directives`
dispatch the real `coordinator/bin/freeze-review-diff.py` in-process
against a real temp git repo, built with
`build_review_partition_freeze_directives` exactly as
`/workstream-complete`'s own review-partition builder does (never a
hand-assembled directive dict).

`freeze-review-diff.py` resolves its repo root from `os.getcwd()` when
`--repo-root` is absent (`_resolve_repo_root`), and `_load_cli_module`
dispatches the CLI in-process — so this test `monkeypatch.chdir(temp_repo)`
before building the directive, or the freeze would run against whatever
repo happens to be this test process's cwd rather than the fixture. Empty
`judgment_points` are passed to `_execute_directives` so every directive's
gate is open by construction — this test is not exercising the halt-contract
judgment-point gate at all, and a closed gate would land the narrowed slice
in `blocked` rather than `failed`, passing the assertion below for the
wrong reason.

Spec backlink: docs/plans/2026-09-11-close-the-three-silent-failure-gaps.md, chunk P1c

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_freeze_coverage_halts_the_partition.py -q
"""

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
