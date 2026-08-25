"""Tests for `substrate_migrate.WRITE_SURFACE`.

Spec backlink: state/debt-backlog/2026-08-06-write-surface-declarations-must-live-wit-e49b9cfd8ad1.yaml
(writer-declared write-surface manifest — a writer that mutates the machine
declares its own surface rather than leaving it for the caller to restate).

Purpose: proves `substrate_migrate` declares the surfaces it actually
touches — the single-file manifest copy, the SHAPED machine-local tree
copy, the two platform-branched compat-pointer writes (POSIX symlink vs
Windows junction, distinct artifacts, not collapsed into one), and the
DELETE clause for the legacy real directory removed immediately before the
pointer is installed. Deletions count: a consent-free machine mutation
with no paper trail is exactly what this manifest exists to prevent.

Negative spec — this module does NOT:
  - validate `WRITE_SURFACE` against `write_surface.validate()` (the
    emission op's concern, not this writer's);
  - assert on the resolved machine-specific absolute path — every path
    template uses the `<settings_home>`/`<claude_base>` shape placeholders
    the declaration itself uses, never this machine's resolved path;
  - exercise `migrate_substrate_to_settings_home` behaviour — this file is
    declaration-only, no migration behaviour was changed to produce it.
"""
from __future__ import annotations

from coordinator_core.install import substrate_migrate as target
from coordinator_core.install.write_surface import ShapedClause, StaticClause


def test_write_surface_identity_and_clause_count():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "substrate-migrate"
    assert declaration.source_module == "coordinator_core.install.substrate_migrate"
    assert len(declaration.clauses) == 4


def test_manifest_copy_clause_is_static_file_path():
    clause = target.WRITE_SURFACE.clauses[0]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.path == f"<settings_home>/{target.LEGACY_MANIFEST_FILENAME}"
    assert entry.effect == "write"


def test_tree_migration_clause_is_shaped_not_flattened():
    clause = target.WRITE_SURFACE.clauses[1]
    assert isinstance(clause, ShapedClause)
    assert clause.discovered_by == "_migrate_tree"
    assert clause.entry_template.kind == "file-path"
    assert clause.entry_template.path == (
        f"<settings_home>/{target.LEGACY_MACHINE_LOCAL_DIRNAME}/<relative-path>"
    )


def test_compat_pointer_clause_has_distinct_posix_and_windows_entries():
    clause = target.WRITE_SURFACE.clauses[2]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 2
    posix_entry, windows_entry = clause.entries
    for entry in (posix_entry, windows_entry):
        assert entry.kind == "file-path"
        assert entry.path == f"<claude_base>/{target.LEGACY_MACHINE_LOCAL_DIRNAME}"
        assert entry.effect == "write"
    assert posix_entry.reason != windows_entry.reason
    assert "symlink_to" in posix_entry.reason
    assert "mklink /J" in windows_entry.reason


def test_legacy_directory_removal_is_declared_as_delete_clause():
    clause = target.WRITE_SURFACE.clauses[3]
    assert isinstance(clause, StaticClause)
    assert clause.effect == "delete"
    assert len(clause.entries) == 1
    entry = clause.entries[0]
    assert entry.kind == "file-path"
    assert entry.effect == "delete"
    assert entry.path == f"<claude_base>/{target.LEGACY_MACHINE_LOCAL_DIRNAME}"


def test_no_delete_clause_for_the_copied_manifest_or_tree_source():
    """The manifest and tree clauses are copies, not moves — legacy source
    files are left in place, so only the compat-pointer directory
    (clause 4) carries a delete effect."""
    delete_clauses = [c for c in target.WRITE_SURFACE.clauses if c.effect == "delete"]
    assert len(delete_clauses) == 1
    assert delete_clauses[0] is target.WRITE_SURFACE.clauses[3]
