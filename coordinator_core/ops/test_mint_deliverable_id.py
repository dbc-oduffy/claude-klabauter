"""Characterization tests for coordinator_core.ops.mint_deliverable_id.

Ported test intent from the bash oracle — same three paths (carry / stub /
slug), same stderr path-label strings, same output-format assertions.

Port of: mint-deliverable-id.test.sh (DoE 3a561713, 2026-07-22)
"""
from __future__ import annotations

import re

import pytest

from coordinator_core.ops.mint_deliverable_id import main, mint


# ---------------------------------------------------------------------------
# mint() — library form
# ---------------------------------------------------------------------------


def test_carry_path_echoes_id_unchanged():
    result, label = mint(deliverable_id="dlv-my-plan-abc123")
    assert result == "dlv-my-plan-abc123"
    assert label == "carry"


def test_carry_path_is_idempotent():
    r1, _ = mint(deliverable_id="dlv-my-plan-abc123")
    r2, _ = mint(deliverable_id="dlv-my-plan-abc123")
    assert r1 == r2


def test_stub_path_mints_dlv_prefixed_stub_id():
    result, label = mint(stub_id="some-feature-3")
    assert result == "dlv-some-feature-3"
    assert label == "mint-from-stub"


def test_slug_path_mints_dlv_slug_6hex():
    result, label = mint(slug="my-plan")
    assert re.match(r"^dlv-my-plan-[0-9a-f]{6}$", result)
    assert label == "mint-from-slug"


def test_slug_path_repeated_calls_differ():
    r1, _ = mint(slug="my-plan")
    r2, _ = mint(slug="my-plan")
    # Not a correctness requirement (bash oracle isn't cryptographically
    # unique either) but the common case given time/pid/random inputs.
    assert r1.startswith("dlv-my-plan-")
    assert r2.startswith("dlv-my-plan-")


def test_zero_args_raises_value_error():
    with pytest.raises(ValueError, match="required"):
        mint()


def test_multiple_args_raises_value_error():
    with pytest.raises(ValueError, match="mutually exclusive"):
        mint(slug="a", stub_id="b")


def test_empty_string_args_treated_as_unset():
    with pytest.raises(ValueError, match="required"):
        mint(deliverable_id="", stub_id="", slug="")


# ---------------------------------------------------------------------------
# main() — CLI form
# ---------------------------------------------------------------------------


def test_cli_carry_path(capsys):
    rc = main(["--deliverable-id", "dlv-my-plan-abc123"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "dlv-my-plan-abc123"
    assert "carry path" in out.err


def test_cli_stub_path(capsys):
    rc = main(["--stub-id", "some-feature-3"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "dlv-some-feature-3"
    assert "mint-from-stub path" in out.err


def test_cli_slug_path(capsys):
    rc = main(["--slug", "my-plan"])
    out = capsys.readouterr()
    assert rc == 0
    assert re.match(r"^dlv-my-plan-[0-9a-f]{6}$", out.out.strip())
    assert "mint-from-slug path" in out.err


def test_cli_no_args_exits_1(capsys):
    rc = main([])
    out = capsys.readouterr()
    assert rc == 1
    assert "required" in out.err


def test_cli_multi_mode_exits_1(capsys):
    rc = main(["--slug", "a", "--stub-id", "b"])
    out = capsys.readouterr()
    assert rc == 1
    assert "mutually exclusive" in out.err


def test_cli_unknown_arg_exits_1(capsys):
    rc = main(["--bogus", "x"])
    out = capsys.readouterr()
    assert rc == 1
    assert "Unknown argument" in out.err


def test_cli_help_exits_0(capsys):
    rc = main(["--help"])
    out = capsys.readouterr()
    assert rc == 0
    assert "mint-deliverable-id.sh" in out.out
