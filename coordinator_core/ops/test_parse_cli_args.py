"""
Tests for coordinator_core.ops.parse_cli_args.

Covers both the pure-function layer (`parse_flag`, `parse_date_flags`) and
the "cli.parse_flag" / "cli.parse_date_flags" JSON-RPC handlers, including
the fail-loud contract for `--only` without `--for-date` and a double-
invocation idempotency check per AC7.
"""

from __future__ import annotations

import asyncio

import pytest

# ---- Import guard: fires @register_op side-effect for both ops. ----
import coordinator_core.ops.parse_cli_args  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.parse_cli_args import (
    _handler_parse_date_flags,
    _handler_parse_flag,
    parse_date_flags,
    parse_flag,
)


def _run(coro):
    return asyncio.run(coro)


# ---- registration ----


def test_both_ops_registered():
    assert "cli.parse_flag" in _REGISTRY
    assert "cli.parse_date_flags" in _REGISTRY


# ---- parse_flag (pure function) ----


def test_parse_flag_matches_root():
    value, matched = parse_flag("--root /tmp/foo --verbose", ["--root", "--target"])
    assert value == "/tmp/foo"
    assert matched == "--root"


def test_parse_flag_matches_target_alternate_name():
    value, matched = parse_flag("--verbose --target sibling-repo", ["--root", "--target"])
    assert value == "sibling-repo"
    assert matched == "--target"


def test_parse_flag_no_match_returns_none_none():
    value, matched = parse_flag("--verbose --other thing", ["--root", "--target"])
    assert value is None
    assert matched is None


def test_parse_flag_trailing_flag_has_no_value():
    value, matched = parse_flag("--verbose --root", ["--root"])
    assert value is None
    assert matched == "--root"


def test_parse_flag_flag_immediately_followed_by_another_flag_has_no_value():
    value, matched = parse_flag("--root --verbose", ["--root", "--verbose"])
    assert value is None
    assert matched == "--root"


def test_parse_flag_empty_arguments():
    value, matched = parse_flag("", ["--root"])
    assert value is None
    assert matched is None


def test_parse_flag_empty_flag_names():
    value, matched = parse_flag("--root foo", [])
    assert value is None
    assert matched is None


def test_parse_flag_longer_name_not_shadowed_by_prefix():
    value, matched = parse_flag("--for-date 2026-07-22", ["--for", "--for-date"])
    assert value == "2026-07-22"
    assert matched == "--for-date"


def test_parse_flag_is_idempotent_across_two_invocations():
    args = "--root /tmp/foo --verbose"
    first = parse_flag(args, ["--root"])
    second = parse_flag(args, ["--root"])
    assert first == second


# ---- parse_date_flags (pure function) ----


def test_parse_date_flags_for_date_only():
    result = parse_date_flags("--for-date 2026-07-22")
    assert result == {"for_date": "2026-07-22", "only": False}


def test_parse_date_flags_for_date_and_only():
    result = parse_date_flags("--for-date 2026-07-22 --only")
    assert result == {"for_date": "2026-07-22", "only": True}


def test_parse_date_flags_neither_flag():
    result = parse_date_flags("")
    assert result == {"for_date": None, "only": False}


def test_parse_date_flags_only_without_for_date_raises():
    with pytest.raises(ValueError):
        parse_date_flags("--only")


def test_parse_date_flags_is_idempotent_across_two_invocations():
    args = "--for-date 2026-07-22 --only"
    first = parse_date_flags(args)
    second = parse_date_flags(args)
    assert first == second


# ---- cli.parse_flag handler ----


def test_handler_parse_flag_basic():
    result = _run(_handler_parse_flag(
        {"arguments": "--root /tmp/foo", "flag_names": ["--root", "--target"]}
    ))
    assert result == {"value": "/tmp/foo", "matched_flag": "--root"}


def test_handler_parse_flag_missing_params_default_empty():
    result = _run(_handler_parse_flag({}))
    assert result == {"value": None, "matched_flag": None}


def test_handler_parse_flag_double_invocation_is_byte_identical():
    params = {"arguments": "--root /tmp/foo", "flag_names": ["--root"]}
    first = _run(_handler_parse_flag(dict(params)))
    second = _run(_handler_parse_flag(dict(params)))
    assert first == second


# ---- cli.parse_date_flags handler ----


def test_handler_parse_date_flags_basic():
    result = _run(_handler_parse_date_flags({"arguments": "--for-date 2026-07-22 --only"}))
    assert result == {"for_date": "2026-07-22", "only": True}


def test_handler_parse_date_flags_missing_params_default_empty():
    result = _run(_handler_parse_date_flags({}))
    assert result == {"for_date": None, "only": False}


def test_handler_parse_date_flags_only_without_for_date_raises():
    with pytest.raises(ValueError):
        _run(_handler_parse_date_flags({"arguments": "--only"}))


def test_handler_parse_date_flags_double_invocation_is_byte_identical():
    params = {"arguments": "--for-date 2026-07-22"}
    first = _run(_handler_parse_date_flags(dict(params)))
    second = _run(_handler_parse_date_flags(dict(params)))
    assert first == second
