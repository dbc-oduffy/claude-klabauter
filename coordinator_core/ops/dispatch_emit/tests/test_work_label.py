"""The ``work:<row-id>`` grammar, and the emitter's use of it.

The round-trip tests are the cheap half. The one that earns its place is
``test_the_emitter_builds_its_label_through_the_grammar``: without it, the
grammar can be correct and the emitter can still compose its own f-string
beside it, which is exactly the state this module was written to end.
"""

from __future__ import annotations

import inspect

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.work_label import (
    WORK_LABEL_PREFIX,
    build_work_label,
    parse_work_label,
)


@pytest.mark.parametrize("row_id", ["C1", "C12", "c1", "C1a", "P2d", "row-with-dashes"])
def test_build_and_parse_are_inverses(row_id: str):
    assert parse_work_label(build_work_label(row_id)) == row_id


def test_build_carries_the_declared_prefix():
    assert build_work_label("C3") == f"{WORK_LABEL_PREFIX}C3" == "work:C3"


@pytest.mark.parametrize("label", ["C3", "phase:C3", "", "workC3", "Work:C3"])
def test_a_non_chunk_label_parses_to_none(label: str):
    assert parse_work_label(label) is None


def test_a_bare_prefix_is_not_a_chunk():
    """The emitter never emits an empty id, so a consumer meeting one has
    found drift. Returning "" would let it pass as a row id downstream."""
    assert parse_work_label("work:") is None


def test_the_emitter_builds_its_label_through_the_grammar():
    """Both wave-composition call sites go through `build_work_label`, and
    neither reconstructs the token itself. A re-introduced f-string passes
    every other test in this file."""
    source = inspect.getsource(emit._wave_agent_calls)
    assert "build_work_label(row.id)" in source, (
        "the wave composer no longer builds its label through the grammar"
    )
    assert "f'work:" not in source and 'f"work:' not in source, (
        "the wave composer composes a `work:` label itself, beside the grammar "
        "-- that is the drift work_label.py exists to prevent"
    )
