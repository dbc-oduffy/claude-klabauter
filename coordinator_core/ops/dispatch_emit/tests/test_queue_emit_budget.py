"""Pins the learn-lessons run budget at the survivor-limit point of the lessons emit: a queue
costing 3x the cap admits exactly the fitting prefix, refuses nothing, and the next emit takes
the rows after it.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.contract import run_budget
from coordinator_core.learn_lessons_pipeline import grind_cost
from coordinator_core.learn_lessons_pipeline.tests.lesson_grind_fixture import _PROFILE, _row_text
from coordinator_core.ops import backlog_pass_ledger
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script

from .conftest import REVIEW_KW

_ROWS = 24
_BATCH = 4


def _repo(tmp_path: Path, limit: str = "null") -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    queue = repo / "state" / "lessons"
    queue.mkdir(parents=True)
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "lessons.yaml").write_text(_PROFILE.replace("limit: null", f"limit: {limit}"), encoding="utf-8")
    for i in range(_ROWS):
        body = f"A distinct rule number {i} that nobody has written down anywhere else."
        (queue / f"row{i:02d}.yaml").write_text(
            _row_text(created=f"2026-08-{i + 1:02d}", body=body), encoding="utf-8"
        )
    return repo, queue, pdir


def _emit(repo: Path, queue: Path, pdir: Path, run: str):
    run_dir = repo / "state" / "queue-grind" / "lessons" / run
    run_dir.mkdir(parents=True)
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


def _fitting_batches() -> int:
    unit = grind_cost.batch_unit_cost(has_verify=True, prevet_size=0, batch_size=_BATCH)
    return run_budget.fit_units(run_budget.load("learn-lessons"), run_budget.UnitCost(), unit, _ROWS // _BATCH)


def test_three_times_the_cap_admits_the_fitting_prefix_and_refuses_nothing(tmp_path):
    repo, queue, pdir = _repo(tmp_path)
    fitting = _fitting_batches()
    assert fitting * 3 <= _ROWS // _BATCH

    emission = _emit(repo, queue, pdir, "run-1")

    admitted = sorted(_batched_row_ids(emission.script))
    assert admitted == [f"row{i:02d}" for i in range(fitting * _BATCH)]
    extras = emission.receipt_extras
    assert extras["admitted_count"] == fitting * _BATCH
    assert extras["deferred"] == _ROWS - fitting * _BATCH
    assert all(extras["budget"]["within"].values())


def test_the_next_emit_takes_the_rows_after_the_prefix(tmp_path):
    repo, queue, pdir = _repo(tmp_path)
    first = sorted(_batched_row_ids(_emit(repo, queue, pdir, "run-1").script))
    for row_id in first:
        (queue / f"{row_id}.yaml").unlink()

    second = _emit(repo, queue, pdir, "run-2")

    taken = sorted(_batched_row_ids(second.script))
    assert taken and not set(taken) & set(first)
    assert taken == [f"row{i:02d}" for i in range(len(first), len(first) + len(taken))]
    summary = backlog_pass_ledger.summary(str(repo), "learn-lessons")
    assert summary["runs"] == 2 and summary["open"] is True
    assert summary["cumulative_tokens"] > 0


def test_a_queue_within_the_cap_defers_nothing_and_closes_the_pass(tmp_path):
    repo, queue, pdir = _repo(tmp_path)
    for i in range(_BATCH, _ROWS):
        (queue / f"row{i:02d}.yaml").unlink()

    emission = _emit(repo, queue, pdir, "run-1")

    assert emission.receipt_extras["deferred"] == 0
    assert len(_batched_row_ids(emission.script)) == _BATCH
    assert backlog_pass_ledger.summary(str(repo), "learn-lessons")["open"] is False


def test_the_profile_limit_still_applies_when_smaller_than_the_fit(tmp_path):
    repo, queue, pdir = _repo(tmp_path, limit="3")

    emission = _emit(repo, queue, pdir, "run-1")

    assert sorted(_batched_row_ids(emission.script)) == ["row00", "row01", "row02"]
    assert emission.receipt_extras["deferred"] == _ROWS - 3
