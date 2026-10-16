"""The pre-dispatch ``check:`` leg is cut; the falsifier-integrity review phase stays.

Pins: an inventory-route script carries no ``check:`` label, ``_alreadyDone`` or
``ALREADY-DONE: pre-dispatch check`` text; the review phase is still composed when
``review_specs`` is passed; ``review_specs`` without ``predispatch=True`` still raises;
the wake-digest ``predispatch`` object keeps ``reviews`` and drops the check-only keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import wake_digest
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import ReviewInput, review_specs
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.session.record_homes import record_path

from .conftest import REVIEW_KW

PLAN = "docs/plans/with-falsifier.md"


def _row(row_id, writes, deps=()):
    return WaveRow(
        id=row_id,
        title=f"title-{row_id}",
        surface="s",
        writes=writes,
        reads=[],
        depends_on=list(deps),
        body=f"Spec: {PLAN} ({row_id})\nSummary: does {row_id}.\n",
    )


WAVES = [[_row("A", ["a.py"])], [_row("B", ["b.py"], deps=["A"])]]


def _specs():
    return review_specs(
        [
            ReviewInput(
                plan_path=PLAN,
                criterion="the thing holds",
                how="run the check",
                baseline_output="fails today",
                expected_when_true="passes",
                baseline_ref=None,
                report_path=None,
                report_json=None,
            )
        ]
    )


def _compose(**kw):
    return compose_script(
        WAVES,
        name="wf",
        description="inventory",
        plan_path=Path(record_path(".", "mise-inventory", "x.spine.md")).as_posix(),
        predispatch=True,
        review_specs=_specs(),
        **REVIEW_KW,
        **kw,
    )


def test_inventory_script_carries_no_check_leg_text():
    script = _compose()
    assert "label: 'check:" not in script
    assert "_alreadyDone" not in script
    assert "ALREADY-DONE: pre-dispatch check" not in script


def test_inventory_script_keeps_the_falsifier_integrity_phase():
    script = _compose()
    assert "label: 'falsifier-integrity:1'" in script


def test_review_specs_without_predispatch_still_raises():
    with pytest.raises(ValueError, match="review_specs requires predispatch=True"):
        compose_script(
            WAVES,
            name="wf",
            description="d",
            plan_path="p.md",
            review_specs=_specs(),
            predispatch=False,
            **REVIEW_KW,
        )


def test_digest_schema_predispatch_object_drops_the_check_keys():
    props = wake_digest.load_schema()["properties"]["predispatch"]
    obj = next(o for o in props["anyOf"] if o.get("type") == "object")
    assert "reviews" in obj["properties"]
    for key in ("checks_run", "already_done", "unusable_checks"):
        assert key not in obj["properties"]
        assert key not in obj["required"]
    assert "predispatch_check_result" not in wake_digest.load_schema()["$defs"]
