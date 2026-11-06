"""Standing gate: the validate-commit Bash-guard leg on the pinned machine-a rows.

THRESHOLD. DR-344's one bar is 500ms process time; the leg is measured as its own thread CPU on a warm
second call (`validate_commit_process_ms`), never wall clock (DR-356: wall clock measures peer load).
The bar is `validate_commit_machine_a_replay.BAR_MS` (a pinned row file carries the same `bar_ms`), not a
second copy here. Spawn count is the leg's git Popen count (`validate_commit_spawns`); a leg whose spawns
grow with staged width or tracked-path count is an amplification, so the gate asserts one count across
every pinned row, narrowest to widest.

ROWS. Shapes (staged width, dirty-tree size, tracked-path count, command form) live in
`coordinator_core/benchmarks/baselines/validate-commit-machine-a-rows.json`; each row is built by the replay's
own `measure_row`, so this gate and the falsifier cannot drift. Rows record shape, never absolute paths.

PLATFORM. Pure Python over thread CPU and the replay's isolated clone: no platform skip.

FORM. A pathspec commit is the only form that runs the staged-vs-worktree divergence check, which costs one
scoped `git diff --name-only` the bare form never spawns. The gate therefore asserts one spawn count per
command form across staged width and tracked-path count, and the pathspec count at most one above the bare count.
"""
from __future__ import annotations

import functools
from typing import Any, Dict

import pytest

from coordinator_core.benchmarks import validate_commit_machine_a_replay as replay

_ROWS = replay.load_rows()["rows"]


@functools.lru_cache(maxsize=None)
def _measured(row_id: str) -> Dict[str, Any]:
    row = next(r for r in _ROWS if r["id"] == row_id)
    return replay.measure_row(row, subphases=False)


def test_pinned_rows_exist() -> None:
    assert _ROWS, "validate-commit-machine-a-rows.json pins no rows"


@pytest.mark.parametrize("row_id", [r["id"] for r in _ROWS])
def test_validate_commit_leg_under_bar(row_id: str) -> None:
    m = _measured(row_id)
    assert m["validate_commit_reached"], f"{row_id}: the validate-commit leg did not run (verdict {m['verdict']})"
    assert m["validate_commit_process_ms"] < replay.BAR_MS, (
        f"{row_id}: validate-commit leg {m['validate_commit_process_ms']}ms process time, "
        f"bar {replay.BAR_MS}ms"
    )


def test_validate_commit_spawns_equal_across_rows() -> None:
    by_form: Dict[str, Dict[str, int]] = {}
    for r in _ROWS:
        by_form.setdefault(r["command_form"], {})[r["id"]] = _measured(r["id"])["validate_commit_spawns"]
    for form, spawns in by_form.items():
        assert len(set(spawns.values())) == 1, f"validate-commit spawn count differs across {form} rows: {spawns}"
    if "bare" in by_form and "pathspec" in by_form:
        bare, pathspec = set(by_form["bare"].values()).pop(), set(by_form["pathspec"].values()).pop()
        assert pathspec <= bare + 1, f"pathspec form spawns {pathspec}, bare {bare}: more than the one divergence probe"
