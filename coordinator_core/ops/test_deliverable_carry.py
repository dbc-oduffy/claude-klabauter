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
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-only-abc123", initiative="init-foo")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), None
    )

    assert dlvr_id == "dlv-plan-only-abc123"
    assert initiative_id == "init-foo"


def test_no_plan_predecessor_hit_unchanged_carry(tmp_path):
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-only-xyz789", initiative="init-bar")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, str(predecessor)
    )

    assert dlvr_id == "dlv-predecessor-only-xyz789"
    assert initiative_id == "init-bar"


def test_neither_present_existing_mint_and_dropped_join_paths_unchanged(tmp_path):
    today = datetime.date.today().strftime("%Y%m%d")

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, None
    )
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""

    plan = tmp_path / "plan.md"
    _write_frontmatter(plan)
    with pytest.raises(DroppedDeliverableJoinError):
        resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), None)


def test_plan_and_predecessor_diverge_raises_with_both_values_and_paths(tmp_path):
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


def _tracking_mint(mint_calls):
    def _mint(**kwargs):
        mint_calls.append(kwargs)
        return mint(**kwargs)

    return _mint


def test_ac2_divergence_at_three_plus_enumerates_every_pair(tmp_path):
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    extra_a = tmp_path / "extra_a.md"
    extra_b = tmp_path / "extra_b.md"
    _write_frontmatter(plan, deliverable_id="dlv-rung-plan")
    _write_frontmatter(predecessor, deliverable_id="dlv-rung-predecessor")
    _write_frontmatter(extra_a, deliverable_id="dlv-rung-extra-a")
    _write_frontmatter(extra_b, deliverable_id="dlv-rung-extra-a")

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


def test_fork_with_no_equivalence_mechanism_still_raises(tmp_path):
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
        )


def test_predecessor_is_plan_input_no_id_anywhere_raises(tmp_path):
    predecessor = tmp_path / "plan.md"
    _write_frontmatter(predecessor, plan_id="pln-example-abc123")

    with pytest.raises(DroppedDeliverableJoinError) as excinfo:
        resolve_deliverable_and_initiative(
            read_frontmatter_field,
            mint,
            None,
            str(predecessor),
            predecessor_is_plan_input=True,
        )

    message = str(excinfo.value)
    assert str(predecessor) in message
    assert "plan input" in message


def test_predecessor_is_plan_input_with_id_still_carries(tmp_path):
    predecessor = tmp_path / "plan.md"
    _write_frontmatter(
        predecessor,
        plan_id="pln-example-abc123",
        deliverable_id="dlv-plan-input-carried-xyz789",
        initiative="init-plan-input",
    )

    dlvr_id, initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field,
        mint,
        None,
        str(predecessor),
        predecessor_is_plan_input=True,
    )

    assert dlvr_id == "dlv-plan-input-carried-xyz789"
    assert initiative_id == "init-plan-input"


def test_predecessor_is_plan_input_flag_unset_mints_from_slug_unchanged(tmp_path):
    today = datetime.date.today().strftime("%Y%m%d")
    predecessor = tmp_path / "plan.md"
    _write_frontmatter(predecessor, plan_id="pln-example-abc123")

    dlvr_id, _initiative_id = resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, str(predecessor)
    )

    assert dlvr_id.startswith(f"dlv-{today}-handoff-")


def test_ac6_unreadable_additional_predecessor_leg_degrades_silently(tmp_path):
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
