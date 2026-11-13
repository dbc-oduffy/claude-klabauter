"""Tests for coordinator_core.post_run.h1_normalize."""

from __future__ import annotations

from coordinator_core.post_run.h1_normalize import normalize_file, normalize_paths, normalize_text

FM = "---\ntitle: Real Title\ntype: wiki\n---\n"


def test_missing_h1_inserted_from_title():
    out = normalize_text(FM + "body\n", "slug")
    assert out == FM + "# Real Title\n\nbody\n"


def test_missing_h1_no_frontmatter_uses_slug():
    assert normalize_text("body\n", "my-slug") == "# my-slug\n\nbody\n"


def test_mismatched_h1_rewritten():
    assert normalize_text(FM + "# Other\n\nbody\n", "s") == FM + "# Real Title\n\nbody\n"


def test_duplicate_h1_dropped_or_demoted():
    out = normalize_text(FM + "# Real Title\n\n# Real Title\n\n# Side\n", "s")
    assert out == FM + "# Real Title\n\n\n## Side\n"
    assert sum(1 for ln in out.splitlines() if ln.startswith("# ")) == 1


def test_fenced_headings_untouched():
    text = FM + "# Real Title\n\n```\n# not a heading\n```\n"
    assert normalize_text(text, "s") == text
    fenced_only = FM + "```\n# code\n```\n"
    assert "# Real Title" in normalize_text(fenced_only, "s")
    assert "# code" in normalize_text(fenced_only, "s")


def test_idempotent():
    for text in (FM + "body\n", "# A\n# B\n", FM + "# X\n\n# Y\n"):
        once = normalize_text(text, "s")
        assert normalize_text(once, "s") == once


def test_file_wrapper_and_skip(tmp_path):
    page = tmp_path / "page-one.md"
    page.write_text("text\n", encoding="utf-8")
    idx = tmp_path / "_index.md"
    idx.write_text("text\n", encoding="utf-8")
    assert normalize_paths([page, idx]) == [page]
    assert page.read_text(encoding="utf-8") == "# page-one\n\ntext\n"
    assert idx.read_text(encoding="utf-8") == "text\n"
    assert normalize_file(page) is False


def test_crlf_line_endings_preserved():
    assert normalize_text("# Old\r\n\r\nbody\r\n", "s") == "# s\r\n\r\nbody\r\n"
    assert normalize_text("body\r\n", "s") == "# s\r\n\r\nbody\r\n"
