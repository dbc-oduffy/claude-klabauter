"""`--only-incomplete` skips landed ids the spine does not carry (no-hunk DONE rows, rows of
other plans in a mixed run) and emits exactly the rows that are still incomplete."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit.tests.test_only_incomplete_row_subset import (  # noqa: F401
    _chunks,
    _emit,
    _registered,
    _run_text,
    repo,
)

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_foreign_and_closed_landed_ids_are_skipped_and_only_the_unstarted_rows_emit(repo):
    text = _run_text(
        repo,
        "abc checkpoint(wave 1): 4 rows — leaf.C2, leaf.C3, usr.C2, C1, C2, C3\n",
    )

    code, script = _emit(repo, "--only-incomplete", text)

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C4", "C5"}
    assert _chunks(script) == {"C4", "C5"}
