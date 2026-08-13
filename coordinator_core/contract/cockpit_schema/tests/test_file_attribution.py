"""
test_file_attribution — parse/reject tests for the FileAttribution aggregate entity.

Pytest port of coordinator-claude `coordinator/cockpit-contract/test/file-attribution.test.ts`.

FileAttribution is a per-(session_id, file_path) aggregate — one row per
DISTINCT file a session touched, with link_type breakdown counts and
edit-metadata sums (D-FA-SHAPE decision).

Spec backlinks:
  - docs/plans/2026-06-30-ccos-8-cockpit-read-contract-spine-entities.md § C3, AC6
  - docs/wiki/cockpit-contract-entity-addition-protocol.md §Steps
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.file_attribution import (
    FileAttribution,
)
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "coordinator_artifact",
    "repo": ".example-doctrine-mirror-repo",
    "ref": None,
    "path": "~/.claude/projects/-test-project/test-session.jsonl",
    "observed_at": "2026-06-30T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

VALID = {
    "repo": ".example-doctrine-mirror-repo",
    "coordinator_root_path": ".",
    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "file_path": "plugins/coordinator/bin/emit-cockpit-snapshot.sh",
    "edited_count": 3,
    "read_count": 2,
    "referenced_count": 1,
    "lines_added": 42,
    "lines_removed": 7,
    "last_operation": "edit",
    "completeness": "complete",
    "capture_source": "journal_projection",
    "provenance_completeness": "complete",
    "provenance": PROV,
}


def test_valid_record_parses():
    zod_parse(FileAttribution, VALID)


def test_all_nullable_fields_as_null_parses_d9():
    nulled = {**VALID, "lines_added": None, "lines_removed": None, "last_operation": None}
    zod_parse(FileAttribution, nulled)


def test_invalid_last_operation_enum_rejected():
    assert not zod_safe_parse_ok(FileAttribution, {**VALID, "last_operation": "copy"})


def test_invalid_completeness_enum_rejected():
    assert not zod_safe_parse_ok(FileAttribution, {**VALID, "completeness": "COMPLETE"})


def test_invalid_capture_source_enum_rejected():
    assert not zod_safe_parse_ok(FileAttribution, {**VALID, "capture_source": "manual"})


def test_invalid_provenance_completeness_enum_rejected():
    assert not zod_safe_parse_ok(
        FileAttribution, {**VALID, "provenance_completeness": "partial"}
    )


def test_missing_session_id_rejected():
    bad = dict(VALID)
    del bad["session_id"]
    assert not zod_safe_parse_ok(FileAttribution, bad)


def test_missing_file_path_rejected():
    bad = dict(VALID)
    del bad["file_path"]
    assert not zod_safe_parse_ok(FileAttribution, bad)


def test_honesty_markers_preserved():
    """AC6: completeness and capture_source must be present and required."""
    instance = zod_parse(FileAttribution, VALID)
    assert instance.completeness == VALID["completeness"]
    assert instance.capture_source == VALID["capture_source"]
    assert instance.provenance_completeness == VALID["provenance_completeness"]

    missing_completeness = dict(VALID)
    del missing_completeness["completeness"]
    assert not zod_safe_parse_ok(FileAttribution, missing_completeness)

    missing_capture_source = dict(VALID)
    del missing_capture_source["capture_source"]
    assert not zod_safe_parse_ok(FileAttribution, missing_capture_source)


def test_d9_lines_added_omitted_entirely_rejected():
    bad = dict(VALID)
    del bad["lines_added"]
    assert not zod_safe_parse_ok(FileAttribution, bad)


def test_d9_lines_removed_omitted_entirely_rejected():
    bad = dict(VALID)
    del bad["lines_removed"]
    assert not zod_safe_parse_ok(FileAttribution, bad)


def test_all_valid_last_operation_values_accepted():
    for op in ["edit", "create", "delete", "rename", "bash"]:
        assert zod_safe_parse_ok(FileAttribution, {**VALID, "last_operation": op})


def test_all_valid_completeness_values_accepted():
    for c in ["complete", "partial", "unknown"]:
        assert zod_safe_parse_ok(FileAttribution, {**VALID, "completeness": c})


def test_all_valid_capture_source_values_accepted():
    for s in ["journal_projection", "hook_capture", "derived"]:
        assert zod_safe_parse_ok(FileAttribution, {**VALID, "capture_source": s})
