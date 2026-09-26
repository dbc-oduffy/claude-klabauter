
from __future__ import annotations

import pytest

from coordinator_core.daily_branch import (
    format_span_suffix,
    has_remote_prefix,
    is_allowed_branch,
    is_canonical_branch,
    is_work_branch,
    parse_branch_span,
    read_configured_day_branch,
    record_day_branch_designation,
    rename_target,
    sanitize_slug,
    should_prompt_rename,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("machine-a", "machine-a"),
        ("MACHINE-A", "machine-a"),
        ("Hello World", "hello-world"),
        ("a  b   c", "a-b-c"),
        ("--leading-and-trailing--", "leading-and-trailing"),
        ("foo___bar", "foo-bar"),
        ("a.b.c", "a-b-c"),
        ("café", "caf"),
        ("naïve-señor", "na-ve-se-or"),
        ("../../etc/passwd", "etc-passwd"),
        ("a/b\\c", "a-b-c"),
        ("!!!", ""),
        ("", ""),
        ("已经", ""),
        ("v1.2.3", "v1-2-3"),
    ],
)
def test_sanitize_slug(raw, expected):
    assert sanitize_slug(raw) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("work/x", True),
        ("work/m/2026-09-22", True),
        ("work/m/2026-09-22-2", True),
        ("origin/work/m/2026-09-22", True),
        ("main", False),
        ("feature/foo", False),
        ("origin/main", False),
        ("origin/feature/foo", False),
        ("origin/origin/work/x", False),
        ("upstream/work/x", False),
        ("Work/x", False),
        ("", False),
        (None, False),
    ],
)
def test_is_work_branch(name, expected):
    assert is_work_branch(name) is expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("origin/work/m/2026-09-22", True),
        ("work/x", False),
        ("work/m/2026-09-22", False),
        ("work/m/2026-09-22-2", False),
        ("main", False),
        ("feature/foo", False),
        ("origin/main", False),
        ("origin/feature/foo", False),
        ("origin/origin/work/x", False),
        ("upstream/work/x", False),
        ("Work/x", False),
        ("", False),
        (None, False),
    ],
)
def test_has_remote_prefix(name, expected):
    assert has_remote_prefix(name) is expected


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
    assert parse_branch_span("work/machine-a/2026-05-28to03") == (
        "2026-05-28",
        "2026-06-03",
    )


def test_parse_span_year_roll():
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
    assert parse_branch_span("work/m/2026-05-06to06") == ("2026-05-06", "2026-05-06")


@pytest.mark.parametrize(
    "name",
    [
        "feature/foo",
        "work/machine-a/feature-X",
        "hotfix/urgent",
        "main",
        "work/machine-a/2026-13-01",
        "work/machine-a/2026-05-32",
        "work/machine-a/2026-00-15",
        "work/machine-a/2026-05-00",
        "work/machine-a/2026-5-6",
        "work//2026-05-06",
        "work/a/b/2026-05-06",
        "",
    ],
)
def test_parse_rejects(name):
    assert parse_branch_span(name) is None


def test_parse_none_input():
    assert parse_branch_span(None) is None


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "MAIN",
        "work/machine-a/2026-05-06",
        "work/MACHINE-A/2026-05-06",
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


def test_canonical_accepts_lowercase():
    assert is_canonical_branch("work/machine-a/2026-05-06") is True
    assert is_canonical_branch("main") is True


def test_canonical_rejects_mixed_case():
    assert is_allowed_branch("work/MACHINE-A/2026-05-06") is True
    assert is_canonical_branch("work/MACHINE-A/2026-05-06") is False
    assert is_canonical_branch("MAIN") is False


def test_canonical_rejects_disallowed():
    assert is_canonical_branch("feature/foo") is False


def test_format_span_suffix_same_day():
    assert format_span_suffix("2026-05-06", "2026-05-06") == "2026-05-06"


def test_format_span_suffix_span():
    assert format_span_suffix("2026-05-06", "2026-05-09") == "2026-05-06to09"


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


def test_should_prompt_active_not_in_span():
    now = 1_000_000.0
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
    assert should_prompt_rename(
        "work/machine-a/2026-05-06", "2026-05-09", now - 72 * 3600, now_epoch=now
    ) is False


def test_should_not_prompt_unparseable_branch():
    now = 1_000_000.0
    assert should_prompt_rename("feature/foo", "2026-05-09", now, now_epoch=now) is False


def test_should_prompt_span_branch_not_covering_today():
    now = 1_000_000.0
    assert should_prompt_rename(
        "work/machine-a/2026-05-06to09", "2026-05-12", now - 3600, now_epoch=now
    ) is True


def test_is_allowed_accepts_designated_branch_any_shape():
    assert is_allowed_branch("claude/compassionate-pascal-98ncw7", "claude/compassionate-pascal-98ncw7") is True


def test_is_allowed_rejects_non_matching_when_designated():
    assert is_allowed_branch("feature/foo", "claude/compassionate-pascal-98ncw7") is False


def test_is_allowed_unaffected_when_no_designation():
    assert is_allowed_branch("work/machine-a/2026-05-06", None) is True


def test_is_canonical_accepts_designated_branch_verbatim_case():
    assert is_canonical_branch("Claude/Mixed-Case", "Claude/Mixed-Case") is True


def test_is_canonical_still_rejects_mixed_case_work_branch_when_designation_absent():
    assert is_canonical_branch("Work/Machine-A/2026-05-06", None) is False


def _init_bare_gitdir(tmp_path):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    return tmp_path


def test_read_configured_day_branch_absent(tmp_path):
    repo = _init_bare_gitdir(tmp_path)
    assert read_configured_day_branch(repo) is None


def test_record_then_read_configured_day_branch(tmp_path):
    repo = _init_bare_gitdir(tmp_path)
    assert record_day_branch_designation(repo, "claude/compassionate-pascal-98ncw7") is True
    assert read_configured_day_branch(repo) == "claude/compassionate-pascal-98ncw7"


def test_record_is_idempotent(tmp_path):
    repo = _init_bare_gitdir(tmp_path)
    assert record_day_branch_designation(repo, "claude/foo") is True
    assert record_day_branch_designation(repo, "claude/foo") is True
    assert read_configured_day_branch(repo) == "claude/foo"


def test_read_configured_day_branch_no_git_dir(tmp_path):
    assert read_configured_day_branch(tmp_path) is None
