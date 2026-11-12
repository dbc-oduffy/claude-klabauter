"""refresh_cost: fixed inputs give fixed outputs; a skipped census zeroes only the census phase."""
import pytest

from coordinator_core.architecture_survey import refresh_cost


def test_fixed_inputs_give_fixed_outputs():
    got = refresh_cost.estimate(1000, 4, census_skipped=False)
    assert got["label"] == "estimate"
    assert got["phases"] == {
        "census": {"agents": 5, "tokens": 4 * 6_000 + 1000 * 30 + 8_000},
        "analysis": {"agents": 4, "tokens": 4 * 120_000},
        "synthesis": {"agents": 5, "tokens": 4 * 40_000 + 60_000},
    }
    assert got == refresh_cost.estimate(1000, 4, census_skipped=False)


def test_skipped_census_zeroes_only_census():
    before = refresh_cost.estimate(1000, 4, census_skipped=False)
    after = refresh_cost.estimate(1000, 4, census_skipped=True)
    assert after["phases"]["census"] == {"agents": 0, "tokens": 0}
    assert after["phases"]["analysis"] == before["phases"]["analysis"]
    assert after["phases"]["synthesis"] == before["phases"]["synthesis"]
    assert after["total"]["tokens"] == before["total"]["tokens"] - before["phases"]["census"]["tokens"]
    assert after["total"]["agents"] == before["total"]["agents"] - 5
    assert after["label"] == "estimate"


def test_no_buckets_dispatches_nothing_after_census():
    got = refresh_cost.estimate(10, 0, census_skipped=False)
    assert got["total"] == {"agents": 0, "tokens": 0}


def test_negative_rejected():
    with pytest.raises(ValueError):
        refresh_cost.estimate(-1, 0, census_skipped=True)
