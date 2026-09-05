"""
coordinator_core.ops.tests.test_grouping_vocabulary_parity

Pins the grouping vocabulary across every surface that names it, so a member
added on one side cannot sit unreachable on another.

WHY THIS FILE EXISTS. `spun_off` joined the grouping-approval gate on
2026-08-30, when plan.schema.json 2.13.0 was vendored carrying
`grouping_approvals.spun_off`. Three surfaces name the same vocabulary and only
one of them moved:

  - `check_plan_tasks_grouping_approval` (the LINT) widened that day;
  - `plan_tasks_mutate._resolve` (the WRITE GATE) kept keying on the legacy
    frozenset for both legs, so a governed `spun_off` close succeeded and
    minted a record the lint then refused;
  - `plan.tasks.grouping_digest` (the PRODUCER) kept a hand-written
    `choices=["do", "defer", "ruled_out"]`, so the one value an author needs to
    record a PM-assented `spun_off` cut could not be computed at all —
    argparse exited 2 before the handler, which already accepted the grouping,
    ever ran.

Reported from example-cockpit-repo via DoE-claude, 2026-09-04. The recurring failure
is the DESYNC, not the missing member, which is why these assertions are
derived from the constants rather than restating a member list of their own.

No process spawn, no fixture on disk — this runs in the fast tier deliberately.
A desync guard that only fires at a cadence gate is the shape that let this one
sit for five days.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import (
    _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS,
    _PLAN_TASKS_GROUPING_BY_DISPOSITION,
    _PLAN_TASKS_GROUPING_ORDER,
    _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS,
    compute_grouping_digest,
)
from coordinator_core.ops.plan_tasks_grouping_digest import _build_arg_parser


@pytest.mark.parametrize("grouping", _PLAN_TASKS_GROUPING_ORDER)
def test_digest_cli_accepts_every_declared_grouping(grouping):
    """Every member of the order tuple survives the digest CLI's argparse."""
    args = _build_arg_parser().parse_args(
        ["--plan", "docs/plans/x.md", "--grouping", grouping]
    )
    assert args.grouping == grouping


@pytest.mark.parametrize("grouping", _PLAN_TASKS_GROUPING_ORDER)
def test_compute_grouping_digest_accepts_every_declared_grouping(grouping):
    """The producer behind the CLI raises for an unknown grouping, so a member
    it rejects would make the CLI's acceptance hollow."""
    assert compute_grouping_digest([], grouping).startswith("sha256:")


def test_every_disposition_maps_into_a_declared_grouping():
    """A disposition mapped to a grouping outside the order tuple would sort
    nowhere and could never be approved."""
    assert set(_PLAN_TASKS_GROUPING_BY_DISPOSITION.values()) <= set(
        _PLAN_TASKS_GROUPING_ORDER
    )


def test_governed_gated_dispositions_have_an_approval_block_to_answer_with():
    """Every GOVERNED-gated disposition's grouping is declared in the vendored
    plan schema's `grouping_approvals`.

    Gating a grouping the schema cannot carry a block for makes the resolve
    permanently unsatisfiable rather than merely PM-gated — the exact reason
    DR-183's re-gate of `spun_off` had to wait for the 2.13.0 vendor.
    """
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "frontmatter"
            / "schemas"
            / "plan.schema.json"
        ).read_text(encoding="utf-8")
    )
    declared = set(schema["properties"]["grouping_approvals"]["properties"])
    needed = {
        _PLAN_TASKS_GROUPING_BY_DISPOSITION[d]
        for d in _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS
    }
    assert needed <= declared, sorted(needed - declared)


def test_legacy_gated_set_is_a_subset_of_the_governed_one():
    """The two sets are deliberately distinct, but only in one direction: the
    governed leg may gate MORE, never less. A legacy-only member would be a
    disposition a governed plan could close with no ratification at all."""
    assert (
        _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS
        <= _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS
    )
