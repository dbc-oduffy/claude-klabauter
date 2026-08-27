"""Tests for `install_shell_init_guard_seam.WRITE_SURFACE`.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C3f.

Purpose: proves the declaration's markers are DERIVED from `SENTINEL` /
`SENTINEL_END` at test-collection time rather than a copy frozen at
authoring time — a future edit to either constant alone must turn this
test red without any edit to the test itself. Also proves the legacy
(BEGIN-only) entry uses `write_surface.ABSENT_ON_LEGACY_INSTALLS`, not a
second hand-typed spelling of the same fact.

Negative spec — this module does NOT:
    - exercise the rc-block write path on disk (covered by this module's
      own `test_install_shell_init_guard_seam.py`, out of this chunk's
      scope);
    - validate `WRITE_SURFACE` against `write_surface.validate()` (C4's
      emission-op concern, not this writer's).
"""
from __future__ import annotations

from coordinator_core.install.write_surface import ABSENT_ON_LEGACY_INSTALLS, StaticClause
from coordinator_core.ops import install_shell_init_guard_seam as target


def test_write_surface_identity():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "install-shell-init-guard-seam"
    assert declaration.source_module == "coordinator_core.ops.install_shell_init_guard_seam"
    assert len(declaration.clauses) == 1


def test_entries_derived_from_sentinel_constants_not_restated():
    clause = target.WRITE_SURFACE.clauses[0]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 2

    for entry in clause.entries:
        assert entry.kind == "rc-block"
        assert entry.begin_marker == target.SENTINEL

    end_markers = {entry.end_marker for entry in clause.entries}
    assert end_markers == {target.SENTINEL_END, ABSENT_ON_LEGACY_INSTALLS}


def test_legacy_entry_uses_the_shared_sentinel_not_a_second_spelling():
    clause = target.WRITE_SURFACE.clauses[0]
    legacy_entries = [e for e in clause.entries if e.end_marker != target.SENTINEL_END]
    assert len(legacy_entries) == 1
    assert legacy_entries[0].end_marker == ABSENT_ON_LEGACY_INSTALLS
    assert legacy_entries[0].end_marker is ABSENT_ON_LEGACY_INSTALLS
