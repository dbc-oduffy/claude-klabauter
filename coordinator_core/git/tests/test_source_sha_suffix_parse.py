"""parse_source_sha_suffix is the exact inverse of format_source_sha_suffix."""
import pytest

from coordinator_core.git.git_state import (
    format_source_sha_suffix,
    parse_source_sha_suffix,
)


@pytest.mark.parametrize(
    "sha",
    ["a" * 40, "0123456789abcdef0123456789abcdef01234567", "f" * 40, "deadbeef" * 5],
)
def test_round_trip(sha):
    subject = "publish round" + format_source_sha_suffix(sha)
    assert parse_source_sha_suffix(subject) == sha[:12]


def test_trailing_whitespace_tolerated():
    assert parse_source_sha_suffix("x [source-head abcdef1]  \n") == "abcdef1"


@pytest.mark.parametrize(
    "subject",
    [
        "no stamp here",
        "mid [source-head abcdef123456] subject",
        "x [source-head not-hex-zzzz]",
        "",
    ],
)
def test_no_stamp_is_none(subject):
    assert parse_source_sha_suffix(subject) is None
