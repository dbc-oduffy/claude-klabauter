"""
Tests for coordinator_core.telemetry.composition_record (chunk C1,
docs/plans/2026-08-18-arm-the-composition-budget.md).

Covers: `make_fleet_budget` returns a distinct instance per call with
`disposition=SKIP_AND_SURFACE`; the factory propagates the fleet ceilings
(armed at C5) unchanged, a realistic composition stays within the count dial
and a runaway one breaches it; `on_count` fires once per
`record_invocation` and the running count is readable at flush time;
`flush_composition_record` writes exactly one `kind="composition"` row per
call, carrying a non-empty composition name, invocation count, elapsed, and
outcome; a flush from within a `finally` after a simulated directive
exception still writes a record.

Spec backlink: docs/plans/2026-08-18-arm-the-composition-budget.md § C1
               state/subagent-share/3d70362f-6845-47ec-901e-3b9f0b412836/PINNED-INTERFACE.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import composition_budget
from coordinator_core.composition_budget import SKIP_AND_SURFACE, CompositionBudget
from coordinator_core.telemetry import composition_record
from coordinator_core.telemetry.composition_record import (
    _flush_or_raise,
    flush_composition_record,
    make_fleet_budget,
)


@pytest.fixture(autouse=True)
def _sink(tmp_path, monkeypatch):
    """Route op_latency's sink into a tmp git-common-dir stand-in so these
    tests never touch the real repo's on-disk sink, mirroring the house
    pattern in coordinator_core/telemetry/tests/test_op_latency.py."""
    common_dir = tmp_path / ".git"
    common_dir.mkdir()

    def _fake_git_common_dir(repo_root):
        return common_dir

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", _fake_git_common_dir
    )
    sink_path = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    return sink_path


def _read_rows(sink_path: Path) -> list[dict]:
    if not sink_path.exists():
        return []
    with open(sink_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestFactory:
    def test_distinct_instance_per_call(self):
        b1 = make_fleet_budget("baton_assemble")
        b2 = make_fleet_budget("baton_assemble")
        assert b1 is not b2
        assert b1.composition_id != b2.composition_id

    def test_disposition_is_skip_and_surface(self):
        budget = make_fleet_budget("merge_assemble")
        assert budget.disposition == SKIP_AND_SURFACE

    def test_factory_propagates_the_armed_fleet_ceilings(self):
        """The factory hands the module constants through unchanged, which is
        what makes one edit arm all eight compositions at one posture.

        Replaced C1's `test_fleet_ceilings_default_to_none` at C5: that test
        pinned the PURE RECORDER state (both constants `None`), which C5 ended
        on purpose. Pinning the exact values is deliberately NOT done here --
        `test_composition_budget_arming_is_outcome_neutral.py` owns the
        positive-value assertions, and duplicating the literals in a second
        file would mean a future re-derivation has two places to update and
        one of them will be missed."""
        budget = make_fleet_budget("workday_complete")
        assert budget.aggregate_elapsed_budget is composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET
        assert budget.max_invocations is composition_budget.FLEET_MAX_INVOCATIONS

    def test_a_realistic_composition_stays_within_the_armed_count_dial(self):
        """The worst composition observed while deriving the ceilings ran 29
        directives (docs/research/2026-08-18-composition-budget-armed-values.md).
        A budget at that count must not breach -- the guard is sized for
        runaway, not for the healthy tail."""
        budget = make_fleet_budget("workstream_complete")
        for _ in range(29):
            budget.record_invocation()
        assert budget.check() is True
        assert budget.breached_units == ()

    def test_the_armed_count_dial_actually_fires_on_runaway(self):
        """Replaces C1's `test_never_breaches_with_constants_at_none`, whose
        whole claim (500 invocations, no breach) was a property of the ceilings
        being `None`. Now that they are armed the interesting assertion is the
        opposite one: the dial fires, under SKIP_AND_SURFACE, by returning
        False rather than raising."""
        budget = make_fleet_budget("workday_complete")
        for _ in range(500):
            budget.record_invocation()
        assert budget.check() is False
        assert budget.breached_units != ()


class TestOnCountAccumulation:
    def test_on_count_fires_once_per_record_invocation(self):
        budget = make_fleet_budget("pickup_assemble")
        assert budget.invocation_count == 0
        budget.record_invocation()
        assert budget.invocation_count == 1
        budget.record_invocation()
        budget.record_invocation()
        assert budget.invocation_count == 3

    def test_on_count_is_wired_not_none(self):
        budget = make_fleet_budget("consolidate_assemble")
        assert budget.on_count is not None


class TestFlush:
    def test_flush_writes_one_composition_row(self, _sink):
        budget = make_fleet_budget("backlog_grind_assemble")
        budget.record_invocation()
        budget.record_invocation()
        flush_composition_record(budget, "success")

        rows = _read_rows(_sink)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "composition"
        assert row["name"] == "backlog_grind_assemble"
        assert row["composition_id"] == budget.composition_id
        assert row["invocation_count"] == 2
        assert row["outcome"] == "success"
        assert isinstance(row["elapsed_secs"], float)

    def test_flush_from_finally_after_exception_still_writes(self, _sink):
        budget = make_fleet_budget("baton_assemble")
        outcome = "directive_failed"
        try:
            budget.record_invocation()
            raise RuntimeError("simulated directive failure")
        except RuntimeError:
            pass
        finally:
            flush_composition_record(budget, outcome)

        rows = _read_rows(_sink)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "directive_failed"
        assert rows[0]["invocation_count"] == 1

    def test_flush_rejects_invalid_outcome(self, _sink):
        budget = make_fleet_budget("merge_assemble")
        with pytest.raises(ValueError):
            _flush_or_raise(budget, "bogus")

    def test_flush_requires_composition_name(self, _sink):
        bare = CompositionBudget(composition_id="no-name-1")
        with pytest.raises(ValueError):
            _flush_or_raise(bare, "success")

    def test_public_flush_never_raises_and_writes_nothing_on_bad_input(self, _sink):
        """The flush runs from a `finally`; an exception escaping it would replace
        the composition's own exception with a telemetry error. Neither _budget_call
        wraps the flush, so containment lives in flush_composition_record itself."""
        bare = CompositionBudget(composition_id="no-name-2")
        flush_composition_record(bare, "success")
        flush_composition_record(make_fleet_budget("merge_assemble"), "bogus")
        assert _read_rows(_sink) == []

    def test_public_flush_swallows_a_sink_write_failure(self, monkeypatch, _sink):
        def _boom(**kwargs):
            raise OSError("sink unwritable")

        monkeypatch.setattr(composition_record, "record_composition_span", _boom)
        flush_composition_record(make_fleet_budget("merge_assemble"), "success")
