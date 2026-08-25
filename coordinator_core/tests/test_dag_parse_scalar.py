"""
coordinator_core.tests.test_dag_parse_scalar — Tests for dag._parse_scalar's YAML
scalar type-coercion cascade, focused on the non-finite-float guard.

Coverage:
  (a) sha_scientific_notation_stays_str — a short git SHA shaped like \\d+e\\d+
      (e.g. "229e792") is valid scientific notation; float() silently overflows
      it to inf WITHOUT raising, so an unguarded float leg would misparse it as
      a float. Must return the original string instead.
  (b) finite_floats_still_parse — legitimate finite floats (1.5, 1.50, -0.5,
      1e3, .5, 1_0.5) still return the correct float value; the non-finite
      guard must not tighten the finite path with a round-trip str() check.
  (c) non_finite_bare_words_stay_str — "nan"/"inf"/"-inf" bare words fall
      through to string handling (same non-finite rejection as the overflow
      case), matching the new behavior established by the finite guard.
  (d) other_scalar_kinds_unaffected — int, bool, null, and quoted-string
      handling are unchanged by the float-leg guard; asserts exact types
      (int vs bool vs float are distinct in Python and must not be conflated).

Spec backlink: root-caused a fleet-wide artifact.emit wedge — shipped_in: 229e792
parsed to float('inf'), which then hit `subprocess.run([..., sha, ...])` with a
non-str argv member (TypeError). See coordinator_core/ops/roadmap_dag.py and
coordinator_core/ops/emit/resolvers.py for the downstream carry/verify path.
"""

from __future__ import annotations

import pytest

from coordinator_core.dag import _parse_scalar


# ---------------------------------------------------------------------------
# (a) The bug this guard exists to fix
# ---------------------------------------------------------------------------


def test_sha_scientific_notation_stays_str():
    """"229e792" is a short git SHA that also parses as valid scientific
    notation; float("229e792") overflows to inf without raising. The parser
    must reject the non-finite result and fall through to string handling."""
    result = _parse_scalar("229e792")
    assert type(result) is str
    assert result == "229e792"


@pytest.mark.parametrize(
    "text",
    [
        "229e792",
        "1e400",
        "-1e400",
        "9999999999e9999999999",
    ],
)
def test_overflowing_decimal_literals_stay_str(text):
    """Any decimal literal that overflows float() to +/-inf stays a string —
    never a float, regardless of sign or which side of the cascade produced it."""
    result = _parse_scalar(text)
    assert type(result) is str
    assert result == text


# ---------------------------------------------------------------------------
# (b) Finite floats — the guard must not tighten this path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.5", 1.5),
        ("1.50", 1.5),
        ("-0.5", -0.5),
        ("1e3", 1000.0),
        (".5", 0.5),
        ("1_0.5", 10.5),
    ],
)
def test_finite_floats_still_parse(text, expected):
    """Legitimate finite floats — including trailing-zero and underscore-separated
    forms — still parse correctly. A round-trip str(f) == text check would wrongly
    reject "1.50", so the guard must be finiteness-only, not a round-trip check."""
    result = _parse_scalar(text)
    assert type(result) is float
    assert result == expected


# ---------------------------------------------------------------------------
# (c) Non-finite bare words — same rejection, different spelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "+inf", "NaN", "Infinity"])
def test_non_finite_bare_words_stay_str(text):
    """Bare non-finite spellings that Python's float() accepts (nan/inf/Infinity,
    case-insensitive) must also fall through to string handling — the guard is
    finiteness-based, not limited to the decimal-overflow case."""
    result = _parse_scalar(text)
    assert type(result) is str
    assert result == text


# ---------------------------------------------------------------------------
# (d) Other scalar kinds — unaffected by the float-leg guard
# ---------------------------------------------------------------------------


def test_int_still_returns_int():
    result = _parse_scalar("42")
    assert type(result) is int
    assert result == 42


@pytest.mark.parametrize("text, expected", [("true", True), ("false", False)])
def test_bool_still_returns_bool(text, expected):
    result = _parse_scalar(text)
    assert type(result) is bool
    assert result is expected


@pytest.mark.parametrize("text", ["null", "~", ""])
def test_null_forms_return_none(text):
    assert _parse_scalar(text) is None


def test_double_quoted_string_strips_quotes():
    result = _parse_scalar('"229e792"')
    assert type(result) is str
    assert result == "229e792"


def test_single_quoted_string_strips_quotes_and_unescapes():
    result = _parse_scalar("'it''s a sha'")
    assert type(result) is str
    assert result == "it's a sha"


def test_int_vs_bool_vs_float_not_conflated():
    """1 (int), True (bool), and 1.0 (float) must remain distinct — Python's
    bool is an int subclass, so an == comparison alone would hide a conflation."""
    as_int = _parse_scalar("1")
    as_bool = _parse_scalar("true")
    as_float = _parse_scalar("1.0")
    assert type(as_int) is int and as_int == 1
    assert type(as_bool) is bool and as_bool is True
    assert type(as_float) is float and as_float == 1.0
