"""
test_orphaned_schema_refusal — proves `assert_no_orphaned_schema` actually fires.

Subject: the defect DoE-claude found during the 4.0.0 `file_attribution` drop.
`emit_schemas` writes one file per CURRENT registry entity and never prunes, so a
retired entity's `*.schema.json` survives in the directory the
`cockpit-contract-release` tag publishes — well-formed, unmarked, and
indistinguishable from a live entity's schema to any consumer re-vendoring.

Why this file exists at all: the guard it covers is cheap to satisfy and its
passing condition (no orphan present) is the ordinary state of every clean emit,
so a green run says nothing about whether the check is wired in. That is exactly
the shape `state/lessons/2026-08-23-a-guard-whose-passing-condition-is-cheaper-than-its-claim.md`
names — three guards in this campaign passed green while not checking the thing
they claimed. So the orphan is CONSTRUCTED here, not waited for.

Negative-spec: none of these assert that the orphan is deleted. The guard refuses
and leaves the file alone, by design — `out_dir` is routinely a tree this
generator does not own.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas


def test_orphaned_schema_file_is_refused(tmp_path):
    """An emit into a dir holding a retired entity's schema must raise, naming the file."""
    out_dir = tmp_path / "schema"
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)

    orphan = out_dir / "file-attribution.schema.json"
    orphan.write_text(json.dumps({"title": "FileAttribution"}) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)

    assert "file-attribution.schema.json" in str(excinfo.value)


def test_refusal_does_not_delete_the_orphan(tmp_path):
    """The guard refuses; removing the file stays a deliberate act by that tree's owner."""
    out_dir = tmp_path / "schema"
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)

    orphan = out_dir / "retired-entity.schema.json"
    orphan.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)

    assert orphan.exists(), "the generator must not delete a file it did not write"


def test_clean_emit_is_not_refused(tmp_path):
    """The bundle is not itself an orphan — guards against the check eating its own output."""
    out_dir = tmp_path / "schema"
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)


def test_non_schema_files_are_left_alone(tmp_path):
    """Only `*.schema.json` is in scope — a README or fixture beside them is not an orphan."""
    out_dir = tmp_path / "schema"
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)
    (out_dir / "README.md").write_text("not a schema\n", encoding="utf-8")

    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)
