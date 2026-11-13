from coordinator_core.distill.prompt_tier import (
    BYTES_PER_TOKEN,
    LONG_PROMPT_TIER_TOKENS,
    estimate_tokens,
    over_tier,
    split_to_tier,
)

TIER_BYTES = LONG_PROMPT_TIER_TOKENS * BYTES_PER_TOKEN


def test_constants():
    assert LONG_PROMPT_TIER_TOKENS == 100_000
    assert BYTES_PER_TOKEN == 4


def test_estimate_tokens_rounds_up_and_clamps():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(4) == 1
    assert estimate_tokens(5) == 2
    assert estimate_tokens(-9) == 0


def test_over_tier_both_sides_of_the_boundary():
    assert over_tier(LONG_PROMPT_TIER_TOKENS + 1)
    assert not over_tier(LONG_PROMPT_TIER_TOKENS)


def test_split_packs_greedily_under_tier():
    sizes = {"a": TIER_BYTES // 2, "b": TIER_BYTES // 2, "c": 4}
    groups = split_to_tier(["a", "b", "c"], sizes.__getitem__)
    assert groups == [["a", "b"], ["c"]]
    for g in groups:
        assert not over_tier(sum(estimate_tokens(sizes[i]) for i in g))


def test_split_keeps_oversize_item_alone_and_never_drops():
    sizes = {"a": 40, "big": TIER_BYTES * 2, "b": 40}
    groups = split_to_tier(["a", "big", "b"], sizes.__getitem__)
    assert groups == [["a"], ["big"], ["b"]]
    assert [i for g in groups for i in g] == ["a", "big", "b"]


def test_split_empty():
    assert split_to_tier([], len) == []
