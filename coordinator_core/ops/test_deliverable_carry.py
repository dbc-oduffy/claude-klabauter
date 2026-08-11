"""Tests for coordinator_core.ops.deliverable_carry.

Purpose: covers the plan/predecessor carry-or-mint cascade directly at the
engine layer -- `coordinator/bin/tests/test_handoff_deliverable_carry.py`
covers the same function transitively via the CLI trampoline plus the CLI's
own subprocess-level exit codes; this file exercises
`resolve_deliverable_and_initiative` in-process, including the
`DivergentDeliverableIdError` fail-loud path this module adds.

Spec backlink: docs/decisions/DR-207-deliverable-spine-initiative-entity.md
               DD#1 (earliest-artifact tiebreak)
               coordinator_core/contract/commit-trailer-producer-contract.md
               § 1.2 (two independent producers of the same FK)
               cross-repo/inbox/2026-08-01-example-market-data-repo-em-deliverable-id
               -two-producers-diverge-by-value.md (live incident this closes)
"""
from __future__ import annotations

import datetime

import pytest

from coordinator_core.ops.deliverable_carry import (
    DivergentDeliverableIdError,
    DroppedDeliverableJoinError,
    resolve_deliverable_and_initiative,
)
from coordinator_core.ops.mint_deliverable_id import mint
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_plan_and_predecessor_agree_byte_identical_to_no_divergence(tmp_path):
    """(i) plan and predecessor agree -> no raise, same result as a plan-only hit."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-shared-thing-abc123", initiative="init-foo")
    _write_frontmatter(predecessor, deliverable_id="dlv-shared-thing-abc123", initiative="init-bar")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), str(predecessor)
    )

    assert dlvr_id == "dlv-shared-thing-abc123"
    assert initiative_id == "init-foo"


def test_plan_hit_no_predecessor_unchanged_carry(tmp_path):
    """(ii) plan hit, no predecessor -> unchanged carry."""
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-only-abc123", initiative="init-foo")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), None
    )

    assert dlvr_id == "dlv-plan-only-abc123"
    assert initiative_id == "init-foo"


def test_no_plan_predecessor_hit_unchanged_carry(tmp_path):
    """(iii) no plan, predecessor hit -> unchanged carry."""
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-only-xyz789", initiative="init-bar")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, str(predecessor)
    )

    assert dlvr_id == "dlv-predecessor-only-xyz789"
    assert initiative_id == "init-bar"


def test_neither_present_existing_mint_and_dropped_join_paths_unchanged(tmp_path):
    """(iv) neither rung present -> existing DroppedDeliverableJoinError / mint path
    unchanged."""
    today = datetime.date.today().strftime("%Y%m%d")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, None
    )
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""

    plan = tmp_path / "plan.md"
    _write_frontmatter(plan)  # no deliverable_id
    with pytest.raises(DroppedDeliverableJoinError):
        resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), None)


def test_plan_and_predecessor_diverge_raises_with_both_values_and_paths(tmp_path):
    """(v) both present and differing -> DivergentDeliverableIdError naming both
    values and both source paths."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-qsent-03b-multi-adapter-utterance-produc-22b48d")
    _write_frontmatter(predecessor, deliverable_id="dlv-qsent-03b")

    with pytest.raises(DivergentDeliverableIdError) as excinfo:
        resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), str(predecessor))

    message = str(excinfo.value)
    assert "dlv-qsent-03b-multi-adapter-utterance-produc-22b48d" in message
    assert "dlv-qsent-03b" in message
    assert str(plan) in message
    assert str(predecessor) in message
    assert "DR-207 DD#1" in message
    assert "EARLIEST artifact" in message


def test_divergent_join_does_not_pick_a_winner(tmp_path):
    """(vi) the divergent path returns/mutates nothing -- no id is silently chosen."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-side-111")
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-side-222")

    mint_calls = []

    def _tracking_mint(**kwargs):
        mint_calls.append(kwargs)
        return mint(**kwargs)

    with pytest.raises(DivergentDeliverableIdError):
        resolve_deliverable_and_initiative(
            read_frontmatter_field, _tracking_mint, str(plan), str(predecessor)
        )

    assert mint_calls == []


# ---------------------------------------------------------------------------
# N-rung widening (sedge-01, succession-edge-cardinality roadmap): the cases
# below exercise `additional_predecessors` and `equivalence_map`, added
# alongside the original 6 (unedited above) rather than folded into them --
# AC7 requires those 6 pass byte-identically, so they stay untouched.
# ---------------------------------------------------------------------------


def _tracking_mint(mint_calls):
    def _mint(**kwargs):
        mint_calls.append(kwargs)
        return mint(**kwargs)

    return _mint


def test_ac2_divergence_at_three_plus_enumerates_every_pair(tmp_path):
    """AC2: a raise from 3+ diverging rungs enumerates every diverging
    (path, id) pair, not merely the first two."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra_a = tmp_path / "extra_a.md"
    extra_b = tmp_path / "extra_b.md"
    _write_frontmatter(plan, deliverable_id="dlv-rung-plan")
    _write_frontmatter(predecessor, deliverable_id="dlv-rung-predecessor")
    _write_frontmatter(extra_a, deliverable_id="dlv-rung-extra-a")
    _write_frontmatter(extra_b, deliverable_id="dlv-rung-extra-a")  # agrees with extra_a

    with pytest.raises(DivergentDeliverableIdError) as excinfo:
        resolve_deliverable_and_initiative(
            read_frontmatter_field,
            mint,
            str(plan),
            str(predecessor),
            additional_predecessors=[str(extra_a), str(extra_b)],
        )

    message = str(excinfo.value)
    assert "dlv-rung-plan" in message
    assert "dlv-rung-predecessor" in message
    assert "dlv-rung-extra-a" in message
    assert str(plan) in message
    assert str(predecessor) in message
    assert str(extra_a) in message
    assert str(extra_b) in message


def test_ac3_mint_never_called_on_divergent_path_at_any_arity(tmp_path):
    """AC3: generalizes test (vi) to N rungs -- mint is never called
    (transiently or otherwise) on any divergent path at any arity."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra = tmp_path / "extra.md"
    _write_frontmatter(plan, deliverable_id="dlv-n-plan-side")
    _write_frontmatter(predecessor, deliverable_id="dlv-n-plan-side")
    _write_frontmatter(extra, deliverable_id="dlv-n-extra-side")

    mint_calls: list = []

    with pytest.raises(DivergentDeliverableIdError):
        resolve_deliverable_and_initiative(
            read_frontmatter_field,
            _tracking_mint(mint_calls),
            str(plan),
            str(predecessor),
            additional_predecessors=[str(extra)],
        )

    assert mint_calls == []


def test_agreement_at_n_stays_byte_identical(tmp_path):
    """AC4: every rung (plan, predecessor, 2 additional predecessors) naming
    the SAME deliverable_id -> no raise, same carry/initiative precedence as
    the 2-rung agreement case."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra_a = tmp_path / "extra_a.md"
    extra_b = tmp_path / "extra_b.md"
    _write_frontmatter(plan, deliverable_id="dlv-n-agree-abc123", initiative="init-plan")
    _write_frontmatter(predecessor, deliverable_id="dlv-n-agree-abc123", initiative="init-pred")
    _write_frontmatter(extra_a, deliverable_id="dlv-n-agree-abc123")
    _write_frontmatter(extra_b, deliverable_id="dlv-n-agree-abc123")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field,
        mint,
        str(plan),
        str(predecessor),
        additional_predecessors=[str(extra_a), str(extra_b)],
    )

    assert dlvr_id == "dlv-n-agree-abc123"
    assert initiative_id == "init-plan"


def test_ordering_independence_of_the_raise(tmp_path):
    """Divergence is caught regardless of the order the diverging legs are
    listed in `additional_predecessors`."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra_a = tmp_path / "extra_a.md"
    extra_b = tmp_path / "extra_b.md"
    _write_frontmatter(plan, deliverable_id="dlv-order-shared")
    _write_frontmatter(predecessor, deliverable_id="dlv-order-shared")
    _write_frontmatter(extra_a, deliverable_id="dlv-order-shared")
    _write_frontmatter(extra_b, deliverable_id="dlv-order-DIFFERENT")

    for ordering in ([str(extra_a), str(extra_b)], [str(extra_b), str(extra_a)]):
        with pytest.raises(DivergentDeliverableIdError) as excinfo:
            resolve_deliverable_and_initiative(
                read_frontmatter_field,
                mint,
                str(plan),
                str(predecessor),
                additional_predecessors=ordering,
            )
        message = str(excinfo.value)
        assert "dlv-order-shared" in message
        assert "dlv-order-DIFFERENT" in message


def test_ac5_equivalence_map_consulted_declared_fork_no_false_positive(tmp_path):
    """AC5: a declared fork pair (equivalence_map maps the loser id to the
    winner id) does NOT false-positive as a divergence, and the returned/
    minted id stays the RAW winning value -- canonicalize() is read/compare-
    side only, never written back."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra = tmp_path / "extra.md"
    _write_frontmatter(plan, deliverable_id="dlv-fork-winner")
    _write_frontmatter(predecessor, deliverable_id="dlv-fork-winner")
    _write_frontmatter(extra, deliverable_id="dlv-fork-loser")

    dlvr_id, _initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field,
        mint,
        str(plan),
        str(predecessor),
        additional_predecessors=[str(extra)],
        equivalence_map={"dlv-fork-loser": "dlv-fork-winner"},
    )

    # Raw winning value, never canonicalized on the way out.
    assert dlvr_id == "dlv-fork-winner"


def test_equivalence_map_absent_row_still_raises(tmp_path):
    """A fork with NO declared entry in the equivalence map still diverges --
    absence is never treated as a silent merge (deliverable_equivalence.py's
    own negative-spec)."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-unforked-a")
    _write_frontmatter(predecessor, deliverable_id="dlv-unforked-b")

    with pytest.raises(DivergentDeliverableIdError):
        resolve_deliverable_and_initiative(
            read_frontmatter_field,
            mint,
            str(plan),
            str(predecessor),
            equivalence_map={"dlv-some-other-loser": "dlv-some-other-winner"},
        )


def test_ac6_unreadable_additional_predecessor_leg_degrades_silently(tmp_path):
    """AC6: an additional-predecessor path that is not a readable file
    degrades silently to no contribution (matching the plan/predecessor
    rungs' own isfile()-false-to-empty-string degrade) -- no raise, no
    KeyError, no special-cased exception for this arity."""
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    missing_extra = tmp_path / "does-not-exist.md"
    _write_frontmatter(plan, deliverable_id="dlv-degrade-shared", initiative="init-degrade")
    _write_frontmatter(predecessor, deliverable_id="dlv-degrade-shared")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field,
        mint,
        str(plan),
        str(predecessor),
        additional_predecessors=[str(missing_extra)],
    )

    assert dlvr_id == "dlv-degrade-shared"
    assert initiative_id == "init-degrade"
