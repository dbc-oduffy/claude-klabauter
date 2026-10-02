"""entry_point_shim.parse_batch: the one argv splitter both batched dispatchers share."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from entry_point_shim import UnknownTargetError, parse_batch  # noqa: E402

_T = ("alpha", "beta")


def test_args_run_to_the_next_target_name():
    assert parse_batch(["alpha", "x", "y", "beta", "z"], _T) == [
        ("alpha", ["x", "y"]),
        ("beta", ["z"]),
    ]


def test_double_dash_after_a_name_is_consumed_not_forwarded():
    assert parse_batch(["alpha", "--", "x"], _T) == [("alpha", ["x"])]


def test_unknown_name_raises():
    with pytest.raises(UnknownTargetError):
        parse_batch(["gamma"], _T)


def test_empty_argv_is_an_empty_batch():
    assert parse_batch([], _T) == []
