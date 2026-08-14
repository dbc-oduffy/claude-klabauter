"""
test_daily_branch.py — pytest coverage for coordinator_core.daily_branch.

Port of: coordinator-daily-branch.sh (DoE 2fbe0e77, 2026-07-19)
Covers branch-shape cases + sanitization edge cases (spaces, unicode,
path-injection chars).
"""

from __future__ import annotations

import pytest

from coordinator_core.daily_branch import (
    format_span_suffix,
    is_allowed_branch,
    is_canonical_branch,
    parse_branch_span,
    rename_target,
    sanitize_slug,
    should_prompt_rename,
)


# --- sanitize_slug -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("machine-a", "machine-a"),
        ("MACHINE-A", "machine-a"),                 # lowercased
        ("Hello World", "hello-world"),             # space -> dash
        ("a  b   c", "a-b-c"),                       # multi-space collapse
        ("--leading-and-trailing--", "leading-and-trailing"),  # strip edges
        ("foo___bar", "foo-bar"),                    # underscores -> dash, collapsed
        ("a.b.c", "a-b-c"),                           # dots -> dash
        ("café", "caf"),                              # unicode dropped -> stripped
        ("naïve-señor", "na-ve-se-or"),              # unicode runs -> dash
        ("../../etc/passwd", "etc-passwd"),          # path-injection chars -> dash + strip
        ("a/b\\c", "a-b-c"),                          # slash + backslash -> dash
        ("!!!", ""),                                  # all-nonslug -> empty
        ("", ""),                                      # empty in, empty out
        ("已经", ""),                                  # CJK -> empty
        ("v1.2.3", "v1-2-3"),
    ],
)
def test_sanitize_slug(raw, expected):
    assert sanitize_slug(raw) == expected


# --- parse_branch_span -------------------------------------------------------

def test_parse_single_day():
    assert parse_branch_span("work/machine-a/2026-05-06") == (
        "2026-05-06",
        "2026-05-06",
    )


def test_parse_span_same_month():
    assert parse_branch_span("work/machine-a/2026-05-06to09") == (
        "2026-05-06",
        "2026-05-09",
    )


def test_parse_span_month_roll():
    # end-DD (03) < start-DD (28) -> advance month by one.
    assert parse_branch_span("work/machine-a/2026-05-28to03") == (
        "2026-05-28",
        "2026-06-03",
    )


def test_parse_span_year_roll():
    # December -> January, year advances.
    assert parse_branch_span("work/machine-a/2026-12-30to02") == (
        "2026-12-30",
        "2027-01-02",
    )


def test_parse_case_insensitive():
    assert parse_branch_span("WORK/MACHINE-A/2026-05-06") == (
        "2026-05-06",
        "2026-05-06",
    )


def test_parse_end_equal_start_dd():
    # end-DD == start-DD -> same month, no roll.
    assert parse_branch_span("work/m/2026-05-06to06") == ("2026-05-06", "2026-05-06")


@pytest.mark.parametrize(
    "name",
    [
        "feature/foo",
        "work/machine-a/feature-X",
        "hotfix/urgent",
        "main",
        "work/machine-a/2026-13-01",   # month > 12
        "work/machine-a/2026-05-32",   # day > 31
        "work/machine-a/2026-00-15",   # month == 0
        "work/machine-a/2026-05-00",   # day == 0
        "work/machine-a/2026-5-6",     # non-zero-padded
        "work//2026-05-06",            # empty machine segment
        "work/a/b/2026-05-06",         # extra path segment
        "",
    ],
)
def test_parse_rejects(name):
    assert parse_branch_span(name) is None


def test_parse_none_input():
    assert parse_branch_span(None) is None


# --- is_allowed_branch -------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "main",
        "MAIN",
        "work/machine-a/2026-05-06",
        "work/MACHINE-A/2026-05-06",   # mixed case still allowed (shape oracle)
        "work/machine-a/2026-05-06to09",
    ],
)
def test_is_allowed_true(name):
    assert is_allowed_branch(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "feature/foo",
        "work/machine-a/feature-X",
        "hotfix/x",
        "work/machine-a/2026-13-01",
        "",
    ],
)
def test_is_allowed_false(name):
    assert is_allowed_branch(name) is False


# --- is_canonical_branch -----------------------------------------------------

def test_canonical_accepts_lowercase():
    assert is_canonical_branch("work/machine-a/2026-05-06") is True
    assert is_canonical_branch("main") is True


def test_canonical_rejects_mixed_case():
    # Allowed shape, but non-canonical case -> rejected at creation time.
    assert is_allowed_branch("work/MACHINE-A/2026-05-06") is True
    assert is_canonical_branch("work/MACHINE-A/2026-05-06") is False
    assert is_canonical_branch("MAIN") is False


def test_canonical_rejects_disallowed():
    assert is_canonical_branch("feature/foo") is False


# --- format_span_suffix ------------------------------------------------------

def test_format_span_suffix_same_day():
    assert format_span_suffix("2026-05-06", "2026-05-06") == "2026-05-06"


def test_format_span_suffix_span():
    assert format_span_suffix("2026-05-06", "2026-05-09") == "2026-05-06to09"


# --- rename_target -----------------------------------------------------------

def test_rename_target_zero_ahead_is_today_only():
    assert rename_target("machine-a", "2026-05-06", "2026-05-09", 0) == (
        "work/machine-a/2026-05-09"
    )


def test_rename_target_ahead_keeps_span():
    assert rename_target("machine-a", "2026-05-06", "2026-05-09", 3) == (
        "work/machine-a/2026-05-06to09"
    )


def test_rename_target_ahead_same_day_collapses():
    assert rename_target("machine-a", "2026-05-09", "2026-05-09", 3) == (
        "work/machine-a/2026-05-09"
    )


def test_rename_target_accepts_numeric_string():
    assert rename_target("m", "2026-05-06", "2026-05-09", "0") == "work/m/2026-05-09"
    assert rename_target("m", "2026-05-06", "2026-05-09", "2") == "work/m/2026-05-06to09"


@pytest.mark.parametrize("bad", ["", "abc", "-1", "1.5", None])
def test_rename_target_rejects_non_integer(bad):
    with pytest.raises(ValueError):
        rename_target("m", "2026-05-06", "2026-05-09", bad)


# --- should_prompt_rename ----------------------------------------------------

def test_should_prompt_active_not_in_span():
    now = 1_000_000.0
    # Last commit 1h ago (active), branch end != today -> prompt.
    assert should_prompt_rename(
        "work/machine-a/2026-05-06", "2026-05-09", now - 3600, now_epoch=now
    ) is True


def test_should_not_prompt_already_covers_today():
    now = 1_000_000.0
    assert should_prompt_rename(
        "work/machine-a/2026-05-09", "2026-05-09", now - 3600, now_epoch=now
    ) is False


def test_should_not_prompt_stale_commit():
    now = 1_000_000.0
    # Last commit 72h ago -> stale, routes to A/B/C, no prompt.
    assert should_prompt_rename(
        "work/machine-a/2026-05-06", "2026-05-09", now - 72 * 3600, now_epoch=now
    ) is False


def test_should_not_prompt_unparseable_branch():
    now = 1_000_000.0
    assert should_prompt_rename("feature/foo", "2026-05-09", now, now_epoch=now) is False


def test_should_prompt_span_branch_not_covering_today():
    now = 1_000_000.0
    # Span ends 05-09 but today is 05-12, recent commit -> prompt.
    assert should_prompt_rename(
        "work/machine-a/2026-05-06to09", "2026-05-12", now - 3600, now_epoch=now
    ) is True
