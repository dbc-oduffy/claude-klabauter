
from __future__ import annotations

from coordinator_core.dag import _parse_yaml_list_block


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


def test_scalar_list_unaffected():
    lines = [
        "  - strang-06",
        "  - strang-07",
        "  - strang-08",
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == ["strang-06", "strang-07", "strang-08"]
    assert all(isinstance(x, str) for x in result)


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


def test_quoted_value_containing_colon():
    lines = [
        '  - carry_id: cf-alpha-123',
        '    description: "a: b"',
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [{"carry_id": "cf-alpha-123", "description": "a: b"}]


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


def test_ragged_indentation_within_entry_tolerated():
    lines = [
        "  - carry_id: cf-alpha-123",
        "    carry_count: 1",
        "     disposition: carried",
    ]
    result = _parse_yaml_list_block(lines, 2)
    assert result == [
        {"carry_id": "cf-alpha-123", "carry_count": 1, "disposition": "carried"}
    ]
