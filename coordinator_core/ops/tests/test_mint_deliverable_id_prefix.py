"""Tests for the prefix-parameterised minter and the title slugger."""

import re

import pytest

from coordinator_core.ops.mint_deliverable_id import mint, mint_artifact_id, slug_from_title

GOLDEN = [
    ("Hello World", "hello-world"),
    ("  --Hello,   World!!  ", "hello-world"),
    ("", ""),
    # 40-char cut lands inside a word: cut back to the last dash.
    ("alpha bravo charlie delta echo foxtrot golf hotel", "alpha-bravo-charlie-delta-echo-foxtrot"),
    # 40-char cut lands exactly on a separator: no trailing dash.
    ("a" * 39 + " b", "a" * 39),
    # First word alone exceeds 40: hard cut.
    ("x" * 50, "x" * 40),
    # Non-ASCII collapses to separators.
    ("Café résumé naïve — 日本語 plan", "caf-r-sum-na-ve-plan"),
]


@pytest.mark.parametrize("title,expected", GOLDEN)
def test_slug_from_title_golden(title, expected):
    assert slug_from_title(title) == expected


def test_id_shape_and_prefix():
    for title in ("Hello World", "x" * 50, "Café résumé naïve — 日本語 plan"):
        pid = mint_artifact_id("pln", slug_from_title(title))
        assert re.fullmatch(r"pln-[a-z0-9-]+-[0-9a-f]{6}", pid), pid


def test_clamp_on_separator_has_no_double_dash():
    slug = "a" * 29 + "-bcd"
    pid = mint_artifact_id("pln", slug)
    assert "--" not in pid
    assert pid.startswith("pln-" + "a" * 29 + "-")
    assert re.fullmatch(r"pln-a{29}-[0-9a-f]{6}", pid)


def test_slug_part_clamped_to_30():
    pid = mint_artifact_id("pln", "y" * 40)
    assert re.fullmatch(r"pln-y{30}-[0-9a-f]{6}", pid)


def test_two_mints_differ():
    ids = {mint_artifact_id("pln", "same-slug") for _ in range(20)}
    assert len(ids) > 1


def test_mint_slug_and_stub_still_dlv_unclamped():
    mid, label = mint(slug="z" * 40)
    assert label == "mint-from-slug"
    assert re.fullmatch(r"dlv-z{40}-[0-9a-f]{6}", mid)
    sid, label = mint(stub_id="my-stub")
    assert label == "mint-from-stub"
    assert re.fullmatch(r"dlv-my-stub-[0-9a-f]{6}", sid)
    assert mint(deliverable_id="dlv-x-abcdef") == ("dlv-x-abcdef", "carry")
