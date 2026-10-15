"""Ratchet over DoE's live review-roster fragment: rows-per-reviewer ratio, Kira L+ admission,
and a roster that names no delivery verifier."""

import copy

import pytest

from coordinator_core.ops.review_mint import op as review_mint_op
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED, parse_execute_review

_CODE_REVIEWER = "coordinator:code-reviewer"
_KIRA = "coordinator:overengineering-reviewer"
_DELIVERY = "coordinator:delivery-verifier"


@pytest.fixture(scope="module")
def fragment() -> dict:
    try:
        return review_mint_op.load_fragment()
    except FileNotFoundError as exc:
        pytest.skip(f"no content root resolves the live review-roster fragment: {exc}")


def _wave_agents(fragment: dict) -> list:
    stages = fragment["execute_review"]["stages"]
    return next(s for s in stages if s["kind"] == "review-wave")["agents"]


def _schemas(fragment: dict) -> dict:
    names = {
        a["schema"]
        for s in fragment["execute_review"]["stages"]
        for a in s["agents"]
        if "schema" in a
    }
    return {n: {"type": "object"} for n in names}


def _compose_wave(fragment: dict, **kwargs) -> str:
    review = parse_execute_review(fragment, signals={"named": []})
    phases = compose_execute_review(
        review,
        stage_schemas=_schemas(fragment),
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["a.py"],
        **kwargs,
    )
    return dict(phases)["Review wave"]


def test_fragment_pins_three_to_four_rows_per_reviewer(fragment):
    reviewer = next(a for a in _wave_agents(fragment) if a["agentType"] == _CODE_REVIEWER)
    assert reviewer["executor_rows_per_reviewer"] == {"min": 3, "max": 4}
    assert reviewer["per"] == "slice"


def test_fragment_gates_kira_at_tshirt_l(fragment):
    kira = next(a for a in _wave_agents(fragment) if a["agentType"] == _KIRA)
    assert kira["min_tshirt"] == "L"


@pytest.mark.parametrize("slices", [1, 2, 3, 5, 12])
@pytest.mark.parametrize("rows", range(1, 13))
def test_reviewer_count_is_one_per_max_rows_capped_by_slices(fragment, rows, slices):
    review = parse_execute_review(fragment, signals={"named": []})
    agent = next(a for a in review.review_wave if a.agent_type == _CODE_REVIEWER)
    lo, hi = agent.executor_rows_per_reviewer
    assert (lo, hi) == (3, 4)
    expected = max(1, min(slices, -(-rows // hi), rows // lo))
    assert agent.reviewer_count(rows, slices) == expected


@pytest.mark.parametrize("rows", range(1, 13))
def test_composed_wave_groups_slices_by_the_fragment_max(fragment, rows):
    wave = _compose_wave(fragment, executor_rows=rows)
    assert "_groupSlices" in wave
    assert f"_groupSlices(_reviewPrep?.slices ?? [], {rows}, 3, 4)" in wave


@pytest.mark.parametrize("tshirt", ["L", "XL", "XXL"])
def test_kira_composed_at_l_and_above(fragment, tshirt):
    assert _KIRA in _compose_wave(fragment, executor_rows=4, tshirt=tshirt)


@pytest.mark.parametrize("tshirt", ["XS", "S", "M", None])
def test_kira_absent_below_l(fragment, tshirt):
    assert _KIRA not in _compose_wave(fragment, executor_rows=4, tshirt=tshirt)


def test_live_fragment_names_no_delivery_verifier_and_composes_not_rostered(fragment):
    assert _DELIVERY not in {a.get("agentType") for a in _wave_agents(fragment)}
    assert _DELIVERY not in fragment["blocking_verdicts"]
    wave = _compose_wave(fragment, executor_rows=4)
    assert "const _deliveryVerdict = null;" in wave
    assert DELIVERY_NOT_ROSTERED in wave


@pytest.mark.parametrize(
    "field, check",
    [
        ("executor_rows_per_reviewer", test_fragment_pins_three_to_four_rows_per_reviewer),
        ("min_tshirt", test_fragment_gates_kira_at_tshirt_l),
    ],
)
def test_dropping_either_field_fails_the_ratchet(fragment, field, check):
    stripped = copy.deepcopy(fragment)
    for agent in _wave_agents(stripped):
        agent.pop(field, None)
    with pytest.raises(KeyError):
        check(stripped)
