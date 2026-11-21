"""Tests for the reverify scope selector and the pre_existing split."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.reverify_scope import split_pre_existing


pytestmark = [pytest.mark.complete]


_ORACLE_HEAD = "6011210b7d"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pre_existing_red_is_not_charged() -> None:
    out = split_pre_existing(["a::t1", "b::t2"], ["b::t2", "z::t9"])
    assert out == {"failing": ["a::t1"], "pre_existing": ["b::t2"]}


def test_split_with_no_base_failures_charges_all() -> None:
    assert split_pre_existing(["a::t1"], []) == {"failing": ["a::t1"], "pre_existing": []}
