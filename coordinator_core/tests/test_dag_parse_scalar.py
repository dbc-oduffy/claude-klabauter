
from __future__ import annotations

import pytest

from coordinator_core.dag import _parse_scalar


def test_sha_scientific_notation_stays_str():
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
    result = _parse_scalar(text)
    assert type(result) is str
    assert result == text


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
    result = _parse_scalar(text)
    assert type(result) is float
    assert result == expected


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "+inf", "NaN", "Infinity"])
def test_non_finite_bare_words_stay_str(text):
    result = _parse_scalar(text)
    assert type(result) is str
    assert result == text


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
    as_int = _parse_scalar("1")
    as_bool = _parse_scalar("true")
    as_float = _parse_scalar("1.0")
    assert type(as_int) is int and as_int == 1
    assert type(as_bool) is bool and as_bool is True
    assert type(as_float) is float and as_float == 1.0


def test_double_quoted_string_unescapes_like_yaml_does():
    assert _parse_scalar(r'"the PM said \"it always does\""') == 'the PM said "it always does"'
    assert _parse_scalar(r'"a backslash \\ stays one"') == "a backslash \\ stays one"
