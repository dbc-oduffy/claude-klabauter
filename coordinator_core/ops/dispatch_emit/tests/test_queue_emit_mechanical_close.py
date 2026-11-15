"""Pins the default lessons emit: exact duplicates and covered rows close before any LLM sees
them, and the profile limit applies to the survivors, not to the capped set.

Spec backlink: docs/plans/2026-11/2026-11-13-hnd-learn-lessons-spend-60e878.md § Tasks C1.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.learn_lessons_pipeline.tests.lesson_grind_fixture import _PROFILE, RULE, _row_text
from coordinator_core.ops.dispatch_emit import queue_emit
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script

from .conftest import REVIEW_KW

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _lessons_repo(tmp_path: Path, *, limit: int | None) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    queue = repo / "state" / "lessons"
    queue.mkdir(parents=True)
    run_dir = repo / "state" / "queue-grind" / "lessons" / "run-1"
    run_dir.mkdir(parents=True)
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    limit_text = "null" if limit is None else str(limit)
    (pdir / "lessons.yaml").write_text(_PROFILE.replace("limit: null", f"limit: {limit_text}"), encoding="utf-8")
    return repo, queue, run_dir, pdir


def _row(queue: Path, name: str, **fields) -> None:
    (queue / name).write_text(_row_text(**fields), encoding="utf-8")


def _emit(repo: Path, queue: Path, run_dir: Path, pdir: Path):
    return emit_queue_script(
        "lessons",
        "standard",
        None,
        queue=[queue],
        profile_dir=pdir,
        repo_root=repo,
        run_dir=run_dir,
        session_id="sess1",
        **REVIEW_KW,
    )


def _batched_row_ids(script: str) -> list:
    line = next(ln for ln in script.splitlines() if ln.startswith("const BATCHES = "))
    batches = json.loads(line[len("const BATCHES = "):-1])
    return [row if isinstance(row, str) else row["row_id"] for b in batches for row in b["rows"]]


def _distinct(i: int) -> str:
    return f"A distinct rule number {i} that nobody has written down anywhere else."


def test_limit_two_with_three_covered_duplicates_admits_both_distinct_rows(tmp_path):
    repo, queue, run_dir, pdir = _lessons_repo(tmp_path, limit=2)
    archived = repo / "archive" / "lessons" / "2026-09"
    archived.mkdir(parents=True)
    (archived / "old.yaml").write_text(_row_text(status="applied"), encoding="utf-8")
    for i in range(3):
        _row(queue, f"dup{i}.yaml", created=f"2026-08-0{i + 1}")
    _row(queue, "keep-a.yaml", created="2026-08-04", body=_distinct(1))
    _row(queue, "keep-b.yaml", created="2026-08-05", body=_distinct(2))

    emission = _emit(repo, queue, run_dir, pdir)

    assert sorted(_batched_row_ids(emission.script)) == ["keep-a", "keep-b"]
    extras = emission.receipt_extras
    assert extras["admitted_count"] == 2
    assert sorted(c["row_id"] for c in extras["mechanically_closed"]) == ["dup0", "dup1", "dup2"]
    assert {c["covered_by"] for c in extras["mechanically_closed"]} == {"archive/lessons/2026-09/old.yaml"}
    assert sorted(p.name for p in queue.iterdir()) == ["keep-a.yaml", "keep-b.yaml"]
    assert sorted(p.name for p in (repo / "archive" / "lessons").glob("*/dup*.yaml")) == [
        "dup0.yaml",
        "dup1.yaml",
        "dup2.yaml",
    ]


def test_open_duplicates_keep_the_oldest_and_the_limit_counts_survivors(tmp_path):
    repo, queue, run_dir, pdir = _lessons_repo(tmp_path, limit=2)
    for i in range(3):
        _row(queue, f"dup{i}.yaml", created=f"2026-08-0{i + 1}")
    _row(queue, "keep.yaml", created="2026-08-04", body=_distinct(1))
    _row(queue, "later.yaml", created="2026-08-05", body=_distinct(2))

    emission = _emit(repo, queue, run_dir, pdir)

    assert sorted(_batched_row_ids(emission.script)) == ["dup0", "keep"]
    assert emission.receipt_extras["unselected"]["row_ids"] == ["later"]
    assert sorted(c["row_id"] for c in emission.receipt_extras["mechanically_closed"]) == ["dup1", "dup2"]


def test_no_closable_row_changes_nothing_on_disk_and_spawns_nothing(tmp_path, monkeypatch):
    repo, queue, run_dir, pdir = _lessons_repo(tmp_path, limit=None)
    for i in range(5):
        _row(queue, f"r{i}.yaml", body=_distinct(i))
    before = {p.name: p.read_bytes() for p in queue.iterdir()}
    spawned: list = []
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, *a, **k: spawned.append(a))

    emission = _emit(repo, queue, run_dir, pdir)

    assert spawned == []
    assert emission.receipt_extras["mechanically_closed"] == []
    assert {p.name: p.read_bytes() for p in queue.iterdir()} == before
    assert not (repo / "archive").exists()
    assert len(_batched_row_ids(emission.script)) == 5


def test_lessons_emit_selects_the_whole_eligible_set_and_writes_handback_marks(tmp_path, monkeypatch):
    repo, queue, run_dir, pdir = _lessons_repo(tmp_path, limit=2)
    _row(queue, "a.yaml", body=_distinct(1))
    seen: dict = {}
    real = queue_emit.select_rows

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(queue_emit, "select_rows", _spy)
    _emit(repo, queue, run_dir, pdir)
    assert seen["limit"] is None
    assert seen["write_handback_marks"] is True


def test_a_profile_outside_the_mechanical_set_keeps_its_limit_at_selection(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    queue = repo / "state" / "bug-backlog"
    queue.mkdir(parents=True)
    run_dir = repo / "state" / "queue-grind" / "fixture" / "run-1"
    run_dir.mkdir(parents=True)
    for i in range(3):
        (queue / f"row{i}.yaml").write_text(
            f'created: "2026-09-21"\ntitle: "t"\nbody: "{RULE}"\nstatus: "open"\nsurface: "x"\nseverity: "P0"\n',
            encoding="utf-8",
        )
    seen: dict = {}
    real = queue_emit.select_rows

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(queue_emit, "select_rows", _spy)
    emission = emit_queue_script(
        "fixture",
        "standard",
        {"limit": 2},
        queue=[queue],
        profile_dir=_FIXTURE_PROFILE_DIR,
        repo_root=repo,
        run_dir=run_dir,
        session_id="sess1",
        **REVIEW_KW,
    )
    assert seen["limit"] == 2
    assert emission.receipt_extras["mechanically_closed"] == []
    assert len(_batched_row_ids(emission.script)) == 2
    assert sorted(p.name for p in queue.iterdir()) == ["row0.yaml", "row1.yaml", "row2.yaml"]
