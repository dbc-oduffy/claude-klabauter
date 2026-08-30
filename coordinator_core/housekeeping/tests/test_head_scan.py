"""
Tests for coordinator_core.housekeeping.head_scan — the declining
frontmatter head-scan (plan contract 8, chunk C2).

Covers each of the six closed decline triggers individually, the "absent
key is not a decline" case, the happy-path multi-key read, and the
fall-through-to-full-parse path (`scan_keys` falling back to `dag._read_meta`
when `head_scan` declines).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.housekeeping.head_scan import head_scan, scan_keys


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_plain_scalars(tmp_path):
    p = _write(
        tmp_path,
        "happy.md",
        "---\n"
        "status: open\n"
        "deployment_state: ready_to_fire\n"
        "id: some-handoff\n"
        "---\n"
        "body text\n",
    )
    result = head_scan(p, {"status", "deployment_state", "id"})
    assert result == {
        "status": "open",
        "deployment_state": "ready_to_fire",
        "id": "some-handoff",
    }


def test_key_absent_from_frontmatter_is_omitted_not_a_decline(tmp_path):
    p = _write(tmp_path, "partial.md", "---\nstatus: open\n---\nbody\n")
    result = head_scan(p, {"status", "deployment_state"})
    assert result == {"status": "open"}


def test_nested_key_is_not_a_top_level_match(tmp_path):
    p = _write(
        tmp_path,
        "nested.md",
        "---\n"
        "status: open\n"
        "nested:\n"
        "  status: ignored\n"
        "---\n"
        "body\n",
    )
    result = head_scan(p, {"status"})
    assert result == {"status": "open"}


def test_unrequested_duplicate_key_does_not_decline(tmp_path):
    # duplicate on a key nobody asked about must not poison the requested keys
    p = _write(
        tmp_path,
        "dup_unrequested.md",
        "---\nstatus: open\nother: a\nother: b\n---\nbody\n",
    )
    result = head_scan(p, {"status"})
    assert result == {"status": "open"}


# ---------------------------------------------------------------------------
# Decline triggers — each individually
# ---------------------------------------------------------------------------


def test_decline_no_leading_delimiter(tmp_path):
    p = _write(tmp_path, "no_open.md", "status: open\n---\nbody\n")
    assert head_scan(p, {"status"}) is None


def test_decline_no_closing_delimiter_within_budget(tmp_path):
    p = _write(tmp_path, "no_close.md", "---\nstatus: open\nno closing fence here\n")
    assert head_scan(p, {"status"}) is None


def test_decline_tab_in_frontmatter_indentation(tmp_path):
    p = _write(tmp_path, "tabbed.md", "---\nstatus: open\n\tdeployment_state: x\n---\nbody\n")
    assert head_scan(p, {"status", "deployment_state"}) is None


def test_decline_quoted_value(tmp_path):
    p = _write(tmp_path, "quoted.md", '---\nstatus: "open"\n---\nbody\n')
    assert head_scan(p, {"status"}) is None


def test_decline_block_scalar_value(tmp_path):
    p = _write(tmp_path, "block.md", "---\nstatus: |\n  open\n---\nbody\n")
    assert head_scan(p, {"status"}) is None


def test_decline_flow_value(tmp_path):
    p = _write(tmp_path, "flow.md", "---\nblocked_by: [a, b]\n---\nbody\n")
    assert head_scan(p, {"blocked_by"}) is None


def test_decline_anchored_tagged_value(tmp_path):
    p = _write(tmp_path, "anchor.md", "---\nstatus: &anchor open\n---\nbody\n")
    assert head_scan(p, {"status"}) is None


def test_decline_value_containing_hash(tmp_path):
    p = _write(tmp_path, "hash.md", "---\nstatus: open # inline comment\n---\nbody\n")
    assert head_scan(p, {"status"}) is None


def test_decline_duplicate_requested_key(tmp_path):
    p = _write(tmp_path, "dup.md", "---\nstatus: open\nstatus: closed\n---\nbody\n")
    assert head_scan(p, {"status"}) is None


def test_decline_missing_file(tmp_path):
    p = tmp_path / "does_not_exist.md"
    assert head_scan(p, {"status"}) is None


# ---------------------------------------------------------------------------
# Fall-through to full parse
# ---------------------------------------------------------------------------


def test_scan_keys_returns_head_scan_result_when_not_declined(tmp_path):
    p = _write(tmp_path, "plain.md", "---\nstatus: open\n---\nbody\n")
    result = scan_keys(p, {"status"})
    assert result == {"status": "open"}


def test_scan_keys_falls_through_to_full_parse_on_decline(tmp_path):
    # A quoted value declines the head-scan but is a fully legal YAML string
    # that dag._read_meta's full parse resolves without trouble.
    p = _write(tmp_path, "falls_through.md", '---\nstatus: "open"\n---\nbody\n')
    assert head_scan(p, {"status"}) is None
    result = scan_keys(p, {"status"})
    assert result == {"status": "open"}


def test_scan_keys_fall_through_never_returns_a_missing_value(tmp_path):
    # blocked_by is a flow list — head_scan declines, and the fall-through
    # full parse must resolve the real list, never silently omit the key.
    p = _write(tmp_path, "list_falls_through.md", "---\nblocked_by: [a, b]\n---\nbody\n")
    assert head_scan(p, {"blocked_by"}) is None
    result = scan_keys(p, {"blocked_by"})
    assert result == {"blocked_by": ["a", "b"]}


def test_scan_keys_omits_key_absent_after_fall_through(tmp_path):
    p = _write(tmp_path, "absent_after_fallthrough.md", '---\nstatus: "open"\n---\nbody\n')
    result = scan_keys(p, {"status", "deployment_state"})
    assert result == {"status": "open"}
