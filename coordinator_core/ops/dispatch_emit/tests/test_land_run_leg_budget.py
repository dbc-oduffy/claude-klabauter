"""Leg-budget pins for `dispatch.land_run`: sibling spines are read once per op, and the
script_read-to-integrity span spawns nothing. Counts, never wall clock (peer load moves wall)."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof

SIBLINGS = 6


@pytest.fixture(scope="module")
def rows(tmp_path_factory) -> dict:
    out = prof.run(tmp_path_factory.mktemp("lrlb"), SIBLINGS, prof.DEFAULT_TRACKED, prof.DEFAULT_CHAIN, attribute=False)
    return {r.axis: r for r in out}


def test_clean_landing_commits(rows) -> None:
    assert rows[prof.WHOLE_OP].attribution["outcome"] == "committed"


def test_each_sibling_spine_parsed_once_across_the_op(rows) -> None:
    whole = rows[prof.WHOLE_OP]
    assert whole.attribution["sizing_parses"] == SIBLINGS
    assert whole.attribution["plan_parses"] == SIBLINGS


def test_holds_and_spine_legs_do_not_reparse(rows) -> None:
    assert rows[prof.AXIS_SPINE].spine_parses == 2 * SIBLINGS
    assert rows[prof.AXIS_HOLDS].spine_parses == rows[prof.AXIS_SPINE].spine_parses


def test_read_to_integrity_span_is_measured_and_spawns_nothing(rows) -> None:
    span = rows[prof.AXIS_READ_TO_INTEGRITY]
    assert span.attribution["measured"] is True
    assert span.spawns == 0
