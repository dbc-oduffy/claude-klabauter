"""test_sentinel_blocks — pytest port of coordinator/bin/lib/sentinel-blocks.test.js.

Spec backlink: archive/specs/2026-05-01-portable-ideas-from-obsidian-research.md §W2
"""
from __future__ import annotations

from coordinator_core.text.sentinel_blocks import (
    extract_block,
    insert_or_replace_block,
    replace_block,
)

BEGIN = "<!-- BEGIN alpha -->"
END = "<!-- END alpha -->"
BEGIN2 = "<!-- BEGIN beta -->"
END2 = "<!-- END beta -->"


# ---------------------------------------------------------------------------
# extract_block
# ---------------------------------------------------------------------------


def test_extract_block_returns_none_when_begin_marker_absent():
    content = "no markers here\n"
    assert extract_block(content, BEGIN, END) is None


def test_extract_block_returns_none_when_end_marker_absent():
    content = f"before\n{BEGIN}\nsome content\n"
    assert extract_block(content, BEGIN, END) is None


def test_extract_block_extracts_block_between_markers():
    content = f"before\n{BEGIN}\nfoo bar\nbaz\n{END}\nafter\n"
    result = extract_block(content, BEGIN, END)
    assert result is not None
    assert result["block"] == "foo bar\nbaz\n"
    assert "before" in result["before"]
    assert BEGIN in result["before"]
    assert END in result["after"]
    assert "after" in result["after"]


def test_extract_block_empty_block():
    content = f"{BEGIN}\n{END}\n"
    result = extract_block(content, BEGIN, END)
    assert result is not None
    assert result["block"] == ""


# ---------------------------------------------------------------------------
# replace_block
# ---------------------------------------------------------------------------


def test_replace_block_returns_none_when_markers_missing():
    assert replace_block("no markers", BEGIN, END, "new content") is None


def test_replace_block_replaces_block_content_preserves_markers():
    content = f"header\n{BEGIN}\nold content\n{END}\nfooter\n"
    updated = replace_block(content, BEGIN, END, "new content\n")
    assert BEGIN in updated
    assert END in updated
    assert "new content" in updated
    assert "old content" not in updated
    assert "header" in updated
    assert "footer" in updated


def test_replace_block_preserves_before_and_after_content_unchanged():
    before = "line1\nline2\n"
    after = "line3\nline4\n"
    content = before + BEGIN + "\nold\n" + END + "\n" + after
    updated = replace_block(content, BEGIN, END, "replaced\n")
    assert updated.startswith(before)
    assert after in updated


def test_replace_block_adds_trailing_newline_to_new_block_content_if_missing():
    content = f"{BEGIN}\nold\n{END}\n"
    updated = replace_block(content, BEGIN, END, "no-newline")
    # end marker should be on its own line
    lines = updated.split("\n")
    end_idx = next(i for i, l in enumerate(lines) if END in l)
    assert end_idx > 0
    # The line before end marker should be the new content
    assert lines[end_idx - 1] == "no-newline"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_insert_extract_replace_extract_again_content_equal():
    base = "preamble\n"
    # Insert block
    with_block = insert_or_replace_block(base, BEGIN, END, "v1 content\n")
    assert BEGIN in with_block
    assert END in with_block

    # Extract
    ex1 = extract_block(with_block, BEGIN, END)
    assert ex1["block"] == "v1 content\n"

    # Replace
    replaced = replace_block(with_block, BEGIN, END, "v2 content\n")
    assert "v2 content" in replaced
    assert "v1 content" not in replaced

    # Extract again
    ex2 = extract_block(replaced, BEGIN, END)
    assert ex2["block"] == "v2 content\n"


# ---------------------------------------------------------------------------
# insert_or_replace_block
# ---------------------------------------------------------------------------


def test_insert_or_replace_block_inserts_at_end_when_markers_missing():
    content = "existing content\n"
    result = insert_or_replace_block(content, BEGIN, END, "new block\n")
    assert result.startswith("existing content\n")
    assert BEGIN in result
    assert "new block" in result
    assert END in result


def test_insert_or_replace_block_inserts_at_start_when_insert_at_start():
    content = "existing content\n"
    result = insert_or_replace_block(content, BEGIN, END, "new block\n", "start")
    assert result.startswith(BEGIN)
    assert "existing content" in result


def test_insert_or_replace_block_replaces_when_markers_already_exist():
    content = f"before\n{BEGIN}\nold\n{END}\nafter\n"
    result = insert_or_replace_block(content, BEGIN, END, "new\n")
    assert "new" in result
    assert "old" not in result
    assert "before" in result
    assert "after" in result


# ---------------------------------------------------------------------------
# Multiple independent blocks in one file
# ---------------------------------------------------------------------------


def test_multiple_blocks_alpha_and_beta_independently_addressable():
    content = (
        "header\n"
        f"{BEGIN}\nalpha content\n{END}\n"
        "middle\n"
        f"{BEGIN2}\nbeta content\n{END2}\n"
        "footer\n"
    )

    alpha = extract_block(content, BEGIN, END)
    beta = extract_block(content, BEGIN2, END2)

    assert alpha["block"] == "alpha content\n"
    assert beta["block"] == "beta content\n"


def test_multiple_blocks_replacing_alpha_does_not_touch_beta():
    content = f"{BEGIN}\nalpha\n{END}\n" f"{BEGIN2}\nbeta\n{END2}\n"

    updated = replace_block(content, BEGIN, END, "new alpha\n")
    beta = extract_block(updated, BEGIN2, END2)
    assert beta["block"] == "beta\n"
    assert "\nalpha\n" not in updated
    assert "new alpha" in updated


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_replace_block_is_idempotent_when_new_content_equals_existing_content():
    content = f"{BEGIN}\nsame content\n{END}\n"
    once = replace_block(content, BEGIN, END, "same content\n")
    twice = replace_block(once, BEGIN, END, "same content\n")
    assert once == twice
