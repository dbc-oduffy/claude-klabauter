"""Record shapes are frozen and the token proxy is bytes/4."""

import dataclasses

import pytest

from coordinator_core.backlog.record_schema import (
    ArtifactRecord,
    FlaggedChange,
    TopicDigest,
    WeekEntry,
    token_proxy,
)


def test_token_proxy_counts_utf8_bytes_over_four():
    assert token_proxy("") == 0
    assert token_proxy("abcd") == 1
    assert token_proxy("abcdefg") == 1
    assert token_proxy("é" * 4) == 2  # 8 bytes, not 4 characters


@pytest.mark.parametrize(
    "obj",
    [
        FlaggedChange("t", "2026-11-02"),
        WeekEntry("2026-W45"),
        ArtifactRecord("docs/plans/x.md"),
        TopicDigest("k", (), "", 0),
    ],
)
def test_records_are_frozen(obj):
    field = dataclasses.fields(obj)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, "changed")


def test_week_without_summary_is_representable():
    week = WeekEntry("2026-W25", summary_path=None, flagged=(FlaggedChange("x", "2026-06-16", ("abc123",), ("docs/a.md",)),))
    assert week.summary_path is None
    assert week.flagged[0].cited_paths == ("docs/a.md",)


def test_digest_tokens_matches_proxy_of_text():
    text = "## topic\n- decision one\n"
    digest = TopicDigest("topic", (ArtifactRecord("a.md", decisions=("one",)),), text, token_proxy(text))
    assert digest.tokens == len(text.encode("utf-8")) // 4
