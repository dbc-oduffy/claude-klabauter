"""`review.reverify` never classes as foreign a red the run base shows green: with `failing_at_base`
supplied (the base measured) a failing id absent from it is the run's own and stays `unattributed`
(blocking), even when its cause file is outside the review footprint, dirty versus HEAD and unheld."""

from __future__ import annotations

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import (
    PARAM_NODE, SIBLING_FILE, TRACE, _dirty_sibling,
)


def _call(root, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", failing_output={PARAM_NODE: TRACE}, **kw,
    )


def test_base_green_head_red_with_dirty_unheld_cause_is_the_runs_own(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _call(root, failing_at_base=[])
    assert out["foreign_red"] == {} and out["sibling_owned"] == [] and out["foreign"] == []
    assert out["pre_existing"] == [] and out["unattributed"] == [PARAM_NODE]
    assert from_reverify({**out, "failing": [PARAM_NODE]}) != []


def test_base_green_head_red_with_halt_held_cause_is_still_the_runs_own(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=True)
    out = _call(root, failing_at_base=[])
    assert out["sibling_owned"] == [] and out["foreign_red"] == {} and out["unattributed"] == [PARAM_NODE]


def test_red_at_base_stays_pre_existing(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _call(root, failing_at_base=[PARAM_NODE])
    assert out["pre_existing"] == [PARAM_NODE] and out["unattributed"] == []


def test_unmeasured_base_keeps_foreign_red_evidence(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _call(root)
    assert out["foreign_red"] == {PARAM_NODE: [SIBLING_FILE]}
