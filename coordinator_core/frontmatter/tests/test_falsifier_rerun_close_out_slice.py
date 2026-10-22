"""_close_out_slice parses to the full parse restricted to _SLICE_KEYS."""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.falsifier_rerun_report import _SLICE_KEYS, _close_out_slice
from coordinator_core.frontmatter.schema_validate import parse_frontmatter


def _assert_parity(text: str) -> None:
    full = parse_frontmatter(text)["frontmatter"]
    sliced_doc = _close_out_slice(text)
    assert sliced_doc is not None
    sliced = parse_frontmatter(sliced_doc)["frontmatter"]
    expected = {k: v for k, v in (full or {}).items() if k in _SLICE_KEYS} or None
    assert sliced == expected


def test_no_frontmatter_is_none() -> None:
    assert _close_out_slice("# just a heading\n") is None
    assert _close_out_slice("---\nstatus: x\n") is None
    assert _close_out_slice("<!-- unclosed\n---\nstatus: x\n---\n") is None


def test_leading_comment_frontmatter() -> None:
    _assert_parity("\n  <!-- c -->\n<!-- d -->\n---\nstatus: implemented\ntitle: t\n---\nbody\n")


def test_key_absent_slice_parses_to_none() -> None:
    text = "---\ntitle: t\nother: 1\n---\nbody\n"
    assert parse_frontmatter(_close_out_slice(text))["frontmatter"] is None


def test_duplicate_top_level_key_last_wins() -> None:
    _assert_parity("---\nstatus: draft\ntitle: t\nstatus: implemented\n---\n")


def test_zero_indent_sequence_continues_block() -> None:
    text = "---\ngated_exit_criteria:\n- id: a\n  x: 1\n- id: b\ntitle: t\nstatus: ok\n---\n"
    _assert_parity(text)
    assert "id: b" in _close_out_slice(text)
    assert "title" not in _close_out_slice(text)


def test_block_scalar() -> None:
    text = "---\ntitle: |\n  status: fake\nstatus: >\n  folded\n  text\nother: 1\n---\n"
    _assert_parity(text)


def test_interleaved_column0_comment() -> None:
    text = "---\nprime_exit_criterion:\n# note\n  falsifier:\n    how: x\n# trailing\n  y: 1\ntitle: t\n---\n"
    _assert_parity(text)


def test_crlf_input() -> None:
    text = "---\r\nstatus: implemented\r\ntitle: t\r\nexit_criterion_met:\r\n  falsifier_output: ok\r\n---\r\nbody\r\n"
    _assert_parity(text)


def test_longer_key_sharing_prefix_not_matched() -> None:
    text = "---\nstatus_note: x\nstatus: y\n---\n"
    assert "status_note" not in _close_out_slice(text)
    _assert_parity(text)


@pytest.mark.parametrize("key", _SLICE_KEYS)
def test_each_key_selected(key: str) -> None:
    _assert_parity(f"---\n{key}: v\nz: 1\n---\n")
