"""
coordinator_core.tests.test_dag_parse_yaml_list_block — Tests for
dag._parse_yaml_list_block's sequence-of-mappings handling.

Coverage:
  (a) sequence_of_mappings_reproduction — the exact carry_id/carry_count/
      disposition repro from the bug report: every key of every entry must
      survive, not just each entry's first line.
  (b) scalar_list_unaffected — a flat ``- foo`` list of strings still comes
      back as strings (no-regression case; this is the current real usage
      every existing caller depends on).
  (c) mixed_list_supported — a list may mix scalar and mapping-shaped
      entries; each entry is classified independently.
  (d) quoted_value_containing_colon — the classic naive-split failure:
      ``description: "a: b"`` must parse to the value ``a: b``, not be cut
      at the colon inside the quotes.
  (e) empty_list / single_entry_list — degenerate sizes.
  (f) ragged_indentation — continuation lines need only be indented more
      than the entry's own dash, not aligned to a fixed column; documents
      the parser's actual (permissive) contract rather than requiring exact
      YAML-spec column alignment.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb
(the write-side twin of this bug, C0) and
coordinator_core/ops/handoff_gate_aging.py's C6 scope-boundary note, which
was written against the exact silent-truncation bug this module now closes.

Review: code-reviewer / DAG-401 — reproduces the exact truncation reported
against ``_parse_yaml_list_block`` (drops every continuation line of a
sequence-of-mappings entry, keeping only the first key).
"""

from __future__ import annotations

from coordinator_core.dag import _parse_yaml_list_block


# ---------------------------------------------------------------------------
# (a) The bug this fix exists to close
# ---------------------------------------------------------------------------


def test_sequence_of_mappings_reproduction():
    lines = [
        "  - carry_id: cf-alpha-123",
        "    carry_count: 1",
        "    disposition: carried",
        "  - carry_id: cf-beta-456",
        "    carry_count: 2",
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [
        {"carry_id": "cf-alpha-123", "carry_count": 1, "disposition": "carried"},
        {"carry_id": "cf-beta-456", "carry_count": 2},
    ]


# ---------------------------------------------------------------------------
# (b) No-regression: flat scalar lists still parse as strings
# ---------------------------------------------------------------------------


def test_scalar_list_unaffected():
    lines = [
        "  - strang-06",
        "  - strang-07",
        "  - strang-08",
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == ["strang-06", "strang-07", "strang-08"]
    assert all(isinstance(x, str) for x in result)


# ---------------------------------------------------------------------------
# (c) Mixed scalar + mapping entries in one list
# ---------------------------------------------------------------------------


def test_mixed_list_supported():
    lines = [
        "  - strang-06",
        "  - carry_id: cf-alpha-123",
        "    carry_count: 1",
        "  - strang-08",
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [
        "strang-06",
        {"carry_id": "cf-alpha-123", "carry_count": 1},
        "strang-08",
    ]


# ---------------------------------------------------------------------------
# (d) Quoted value containing a colon must not be cut at the inner colon
# ---------------------------------------------------------------------------


def test_quoted_value_containing_colon():
    lines = [
        '  - carry_id: cf-alpha-123',
        '    description: "a: b"',
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [{"carry_id": "cf-alpha-123", "description": "a: b"}]


# ---------------------------------------------------------------------------
# (e) Degenerate sizes
# ---------------------------------------------------------------------------


def test_empty_list():
    assert _parse_yaml_list_block([], 2) == []


def test_single_entry_list_mapping():
    lines = ["  - carry_id: cf-alpha-123", "    carry_count: 1"]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [{"carry_id": "cf-alpha-123", "carry_count": 1}]


def test_single_entry_list_scalar():
    lines = ["  - strang-06"]
    result = _parse_yaml_list_block(lines, 2)
    assert result == ["strang-06"]


# ---------------------------------------------------------------------------
# (f) Ragged indentation across continuation lines within one entry
# ---------------------------------------------------------------------------


def test_ragged_indentation_within_entry_tolerated():
    """Continuation lines need only be MORE indented than the entry's own
    dash (base_indent=2 here) — not aligned to a fixed column. This is the
    parser's actual, permissive contract: a 4-space-indented key and a
    5-space-indented key on the same entry are both accepted as belonging
    to that entry, unlike strict YAML column-alignment rules."""
    lines = [
        "  - carry_id: cf-alpha-123",
        "    carry_count: 1",
        "     disposition: carried",  # one extra space of ragged indent
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [
        {"carry_id": "cf-alpha-123", "carry_count": 1, "disposition": "carried"}
    ]
