"""Ratchet over DoE's live review-roster fragment: rows-per-reviewer ratio, Kira in the integration stage at min_tshirt L,
and a roster that names no delivery verifier."""

import copy

import pytest

from coordinator_core.ops.review_mint import op as review_mint_op
from coordinator_core.ops.review_mint.execute_review import REVIEW_PREP_SLICES_JS, compose_execute_review
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


# Kira placement is pinned only once the live roster carries an integration stage; before that
# these tests skip, so a stale roster leaves the Kira L-floor rule unpinned.
def _integration_agents(fragment: dict) -> list:
    stages = fragment["execute_review"]["stages"]
    return [a for s in stages if s["kind"] == "integration" for a in s["agents"]]


def _has_integration_stage(fragment: dict) -> bool:
    return any(s["kind"] == "integration" for s in fragment["execute_review"]["stages"])


def test_fragment_places_kira_in_integration_stage_at_min_tshirt_l(fragment):
    if not _has_integration_stage(fragment):
        pytest.skip("live roster predates the integration stage (schema v5)")
    kira = next(a for a in _integration_agents(fragment) if a["agentType"] == _KIRA)
    assert kira.get("min_tshirt") == "L"
    assert all(a.get("agentType") != _KIRA for a in _wave_agents(fragment))


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
    assert f"_groupSlices({REVIEW_PREP_SLICES_JS}, {rows}, 3, 4)" in wave


@pytest.mark.parametrize("tshirt", ["L", "XL", "XXL"])
def test_kira_composed_at_l_and_above(fragment, tshirt):
    if _has_integration_stage(fragment):
        pytest.skip("Kira is an integration-stage agent, not a review-wave one")
    assert _KIRA in _compose_wave(fragment, executor_rows=4, tshirt=tshirt)


@pytest.mark.parametrize("tshirt", ["XS", "S", "M", None])
def test_kira_absent_below_l(fragment, tshirt):
    if _has_integration_stage(fragment):
        pytest.skip("Kira is an integration-stage agent, not a review-wave one")
    assert _KIRA not in _compose_wave(fragment, executor_rows=4, tshirt=tshirt)


def test_live_fragment_names_no_delivery_verifier_and_composes_not_rostered(fragment):
    assert _DELIVERY not in {a.get("agentType") for a in _wave_agents(fragment)}
    assert _DELIVERY not in fragment["blocking_verdicts"]
    wave = _compose_wave(fragment, executor_rows=4)
    assert "const _deliveryVerdict = null;" in wave
    assert DELIVERY_NOT_ROSTERED in wave


def test_dropping_rows_per_reviewer_fails_the_ratchet(fragment):
    stripped = copy.deepcopy(fragment)
    for agent in _wave_agents(stripped):
        agent.pop("executor_rows_per_reviewer", None)
    with pytest.raises(KeyError):
        test_fragment_pins_three_to_four_rows_per_reviewer(stripped)


@pytest.mark.parametrize("floor", [None, "M", "XL"])
def test_missing_or_different_kira_floor_fails_the_ratchet(fragment, floor):
    if not _has_integration_stage(fragment):
        pytest.skip("live roster predates the integration stage (schema v5)")
    mutated = copy.deepcopy(fragment)
    for agent in _integration_agents(mutated):
        if agent["agentType"] != _KIRA:
            continue
        if floor is None:
            agent.pop("min_tshirt", None)
        else:
            agent["min_tshirt"] = floor
    with pytest.raises(AssertionError):
        test_fragment_places_kira_in_integration_stage_at_min_tshirt_l(mutated)
