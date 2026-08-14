"""
Tests for coordinator_core.ops.extract_scope_paths.

Byte-parity fixtures lifted from the bash oracle this module ports.

Port of: test-extract-scope-paths.sh (DoE 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.extract_scope_paths import _extract_scope_paths, main


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_multi_path_scope_block(tmp_path, capsys):
    f = _write(
        tmp_path,
        "multi.md",
        """---
plan: docs/plans/2026-06-30-session-terminator-mechanism-unification.md
chunk: C4
scope:
  - bin/extract-scope-paths.sh
  - bin/tests/test-extract-scope-paths.sh
  - docs/wiki/some-doc.md
status: dispatched
---
Body text.
""",
    )
    rc = main([f])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == (
        "bin/extract-scope-paths.sh\n"
        "bin/tests/test-extract-scope-paths.sh\n"
        "docs/wiki/some-doc.md\n"
    )


def test_empty_scope_block(tmp_path, capsys):
    f = _write(
        tmp_path,
        "empty.md",
        """---
plan: docs/plans/test.md
chunk: C1
scope:
status: dispatched
---
""",
    )
    rc = main([f])
    err = capsys.readouterr().err
    assert rc == 1
    assert "scope: block missing or empty in" in err


def test_absent_scope_block(tmp_path, capsys):
    f = _write(
        tmp_path,
        "absent.md",
        """---
plan: docs/plans/test.md
chunk: C1
status: dispatched
---
""",
    )
    rc = main([f])
    err = capsys.readouterr().err
    assert rc == 1
    assert "scope: block missing or empty in" in err


def test_scope_last_field_terminates_at_close_fence(tmp_path, capsys):
    """Scope as the LAST frontmatter field — body `  - ` bullets must not leak."""
    f = _write(
        tmp_path,
        "scope_last.md",
        """---
plan: docs/plans/test.md
chunk: C5
scope:
  - bin/script-a.sh
  - bin/script-b.sh
---
Body section:
  - this body bullet looks like a scope line
  - another body bullet that must not appear in output
""",
    )
    rc = main([f])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "bin/script-a.sh\nbin/script-b.sh\n"


def test_scope_stops_at_next_top_level_key(tmp_path, capsys):
    f = _write(
        tmp_path,
        "next_key.md",
        """---
plan: docs/plans/test.md
chunk: C2
scope:
  - path/to/file-a.md
  - path/to/file-b.py
status: in_flight
commits: []
---
""",
    )
    rc = main([f])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "path/to/file-a.md\npath/to/file-b.py\n"


def test_non_ascii_lowercase_does_not_terminate_scope_block(tmp_path, capsys):
    """ASCII-only `[a-z]` stop condition — matches bash `/^[a-z]/` and
    dirty_tree_gate.py's twin stop condition, NOT str.islower() (which is
    True for non-ASCII lowercase code points like 'ñ')."""
    f = _write(
        tmp_path,
        "non_ascii.md",
        """---
plan: docs/plans/test.md
chunk: C3
scope:
  - bin/script-a.sh
ñote: not a real frontmatter key, starts with non-ASCII lowercase
status: dispatched
---
""",
    )
    rc = main([f])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "bin/script-a.sh\n"


def test_non_default_key_scans_a_different_top_level_list_block():
    """`key` param — a caller can scan `completeness_checklist:` (or any
    top-level list-shaped key) with the same scanner rather than a second
    copy of it."""
    text = (
        "---\n"
        "scope:\n"
        "  - widget.py\n"
        "completeness_checklist:\n"
        '  - "live: the server responds"\n'
        '  - "restart-gated: config reload takes effect"\n'
        "status: dispatched\n"
        "---\n"
    )
    assert _extract_scope_paths(text, key="completeness_checklist") == [
        "live: the server responds",
        "restart-gated: config reload takes effect",
    ]
    # The default key is untouched by scanning past it for a different key.
    assert _extract_scope_paths(text) == ["widget.py"]


def test_quoted_items_are_unquoted():
    """`unquote_yaml_scalar` strips one layer of quoting — a no-op on bare
    `scope:` paths (Finding 6/7 wiring: this scanner now also powers
    `completeness_checklist:`, whose items are quoted strings)."""
    text = '---\nnotes:\n  - "quoted value"\n  - \'single quoted\'\n  - bare-value\nstatus: x\n---\n'
    assert _extract_scope_paths(text, key="notes") == [
        "quoted value",
        "single quoted",
        "bare-value",
    ]


def test_trailing_whitespace_after_closing_quote_is_stripped():
    """The probe-confirmation gate (`completeness_checklist:`) is SECURITY-
    LOAD-BEARING in `coordinator_core.pickup_assemble` — a captured item must
    not silently carry trailing whitespace after its closing quote. Covers
    both the default `scope` key and a `completeness_checklist` key."""
    text = (
        "---\n"
        "scope:\n"
        "  - widget.py   \n"
        "completeness_checklist:\n"
        '  - "live: the server responds"   \n'
        "status: dispatched\n"
        "---\n"
    )
    assert _extract_scope_paths(text) == ["widget.py"]
    assert _extract_scope_paths(text, key="completeness_checklist") == [
        "live: the server responds"
    ]


def test_non_default_key_empty_block_returns_empty_list():
    """Mirrors test_empty_scope_block/test_absent_scope_block but for a
    non-default key — the `completeness_checklist:` consumption path is
    SECURITY-LOAD-BEARING in `coordinator_core.pickup_assemble`, so it gets
    the same missing/empty-block boundary coverage as `scope:`.
    Review: code-reviewer — Finding 2."""
    empty_block = (
        "---\n"
        "plan: docs/plans/test.md\n"
        "chunk: C1\n"
        "completeness_checklist:\n"
        "status: dispatched\n"
        "---\n"
    )
    assert _extract_scope_paths(empty_block, key="completeness_checklist") == []

    absent_block = (
        "---\n"
        "plan: docs/plans/test.md\n"
        "chunk: C1\n"
        "status: dispatched\n"
        "---\n"
    )
    assert _extract_scope_paths(absent_block, key="completeness_checklist") == []


def test_crlf_line_endings_multi_item_and_non_default_key():
    """`text.splitlines()` handles `\\r\\n` uniformly, but nothing asserted
    it for either key before this — a CRLF fixture converts an
    "should work by inspection" claim into a tested one, per the review
    brief's explicit call-out. Review: code-reviewer — Finding 3."""
    text = (
        "---\r\n"
        "scope:\r\n"
        "  - widget.py\r\n"
        "  - bin/other.sh\r\n"
        "completeness_checklist:\r\n"
        '  - "live: the server responds"\r\n'
        "status: dispatched\r\n"
        "---\r\n"
    )
    assert _extract_scope_paths(text) == ["widget.py", "bin/other.sh"]
    assert _extract_scope_paths(text, key="completeness_checklist") == [
        "live: the server responds"
    ]


def test_no_args_returns_2(capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage: extract-scope-paths.sh <handoff-file>" in err


def test_missing_file_returns_2(tmp_path, capsys):
    missing = str(tmp_path / "doesnotexist.md")
    rc = main([missing])
    err = capsys.readouterr().err
    assert rc == 2
    assert "file not found" in err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
