"""The queue emitter composes the execute_review wave after runGrind and refuses without it."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import NoReviewStageError

from .conftest import _V5_ROSTER_FRAGMENT
from .test_queue_emit import _emit


def test_queue_emitter_composes_execute_review(tmp_path):
    script = _emit(tmp_path).script
    grind_at = script.index("await runGrind();")
    prep_at = script.index("const _reviewPrep = ")
    wave_at = script.index("const _reviewWave = ")
    assert grind_at < prep_at < wave_at
    assert script.index("await _adjudicatePmBound();") > wave_at
    assert "if (_reviewPaths.length === 0)" in script


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_roster_fragment": None},
        {"review_stage_schemas": None},
        {"review_roster_fragment": {**_V5_ROSTER_FRAGMENT, "schema_version": 4}},
    ],
    ids=["no-fragment", "no-schemas", "v4-fragment"],
)
def test_queue_emitter_refuses_without_review_inputs(tmp_path, overrides):
    with pytest.raises(NoReviewStageError):
        _emit(tmp_path, **overrides)


def test_queue_review_prep_freezes_from_the_emit_time_head(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import queue_emit

    monkeypatch.setattr(queue_emit, "head_sha", lambda _repo: "c" * 40)
    prep = _emit(tmp_path).script.split("const _reviewPrep = ", 1)[1].split("\n", 1)[0]
    assert "c" * 40 in prep
