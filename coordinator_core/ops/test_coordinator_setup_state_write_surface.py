"""Tests for `coordinator_setup_state.WRITE_SURFACE`.

Spec backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
chunk C3g.

Purpose: proves the declaration's keys are DERIVED from `_MILESTONES` at
test-collection time rather than a copy of the three literal strings frozen
at authoring time — a future addition/removal on `_MILESTONES` alone must
turn this test red without any edit to the test itself.

Negative spec — this module does NOT:
    - exercise `cmd_record`/`_atomic_write` on disk (covered by this
      module's own `test_coordinator_setup_state.py`, out of this chunk's
      scope);
    - validate `WRITE_SURFACE` against `write_surface.validate()` (C4's
      emission-op concern, not this writer's).
"""
from __future__ import annotations

from coordinator_core.install.write_surface import StaticClause
from coordinator_core.ops import coordinator_setup_state as target


def test_write_surface_identity():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "coordinator-setup-state"
    assert declaration.source_module == "coordinator_core.ops.coordinator_setup_state"
    assert len(declaration.clauses) == 1


def test_entries_derived_from_milestones_not_restated():
    clause = target.WRITE_SURFACE.clauses[0]
    assert isinstance(clause, StaticClause)

    expected_keys = {f"{milestone}_at" for milestone in target._MILESTONES}
    declared_keys = {entry.key for entry in clause.entries}
    assert declared_keys == expected_keys
    assert len(clause.entries) == len(target._MILESTONES)

    for entry in clause.entries:
        assert entry.kind == "structured-file-key"
        assert entry.path is not None
