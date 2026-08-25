"""Tests for `install_meta_repo_precommit_hook.WRITE_SURFACE`.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C2 (`install_meta_repo_precommit_hook` third).

Purpose: proves the declaration is DERIVED from `_GATE_REGISTRY`,
`_POST_SYNC_GATE_REGISTRY`, and `_POST_SYNC_HOOK_FILENAMES` at test-collection
time rather than a copy frozen at authoring time — a future edit adding a
gate to either registry, or a filename to the post-sync tuple, must turn
this test red without any edit to the test itself. Also proves the
`hook-gate-region` `begin_marker` byte-matches what `_gate_block()` actually
emits (read back via `_gate_block()` itself, never re-derived by a parallel
format string in the test), and that `file-path` entries cover both
post-sync hook filenames.

Negative spec — this module does NOT:
  - exercise hook installation on disk (covered by the module's own
    `test_install_meta_repo_precommit_hook.py`/`test_install_post_sync_
    hooks.py` siblings, out of this chunk's scope);
  - validate `WRITE_SURFACE` against `write_surface.validate()` (C4's
    emission-op concern, not this writer's).
"""
from __future__ import annotations

import re

from coordinator_core.install.write_surface import StaticClause
from coordinator_core.ops import install_meta_repo_precommit_hook as target


def test_write_surface_identity():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "install-meta-repo-precommit-hook"
    assert declaration.source_module == "coordinator_core.ops.install_meta_repo_precommit_hook"
    assert len(declaration.clauses) == 2


def test_hook_gate_region_entries_derived_from_registries_not_restated():
    """A future 5th `_GATE_REGISTRY`/`_POST_SYNC_GATE_REGISTRY` entry (or a
    removal) must change the declared entry COUNT without touching this
    test — proving the declaration is computed, not a frozen copy."""
    gate_clause = target.WRITE_SURFACE.clauses[0]
    assert isinstance(gate_clause, StaticClause)

    expected_pre_commit = {
        (gate.marker, f"# --- Gate: {gate.label} ({gate.marker}) ---")
        for gate in target._GATE_REGISTRY
    }
    expected_post_sync = {
        (hook_filename, gate.marker, f"# --- Gate: {gate.label} ({gate.marker}) ---")
        for hook_filename in target._POST_SYNC_HOOK_FILENAMES
        for gate in target._POST_SYNC_GATE_REGISTRY
    }

    pre_commit_entries = [e for e in gate_clause.entries if e.path == "pre-commit"]
    post_sync_entries = [e for e in gate_clause.entries if e.path != "pre-commit"]

    assert len(pre_commit_entries) == len(target._GATE_REGISTRY)
    assert len(post_sync_entries) == len(target._POST_SYNC_HOOK_FILENAMES) * len(
        target._POST_SYNC_GATE_REGISTRY
    )

    for entry in gate_clause.entries:
        assert entry.kind == "hook-gate-region"
        assert entry.end_marker is None

    # Compare via the marker string itself extracted with the same regex
    # `_find_gate_region()` uses, rather than trusting our own construction.
    header_re = re.compile(r"^# --- Gate: .*\(([^)]*)\) ---$")
    for entry in pre_commit_entries:
        m = header_re.match(entry.begin_marker)
        assert m is not None
        assert (m.group(1), entry.begin_marker) in expected_pre_commit

    for entry in post_sync_entries:
        m = header_re.match(entry.begin_marker)
        assert m is not None
        assert (entry.path, m.group(1), entry.begin_marker) in expected_post_sync


def test_begin_marker_byte_matches_gate_block_emission():
    """The declared `begin_marker` must byte-match the header line
    `_gate_block()` actually renders — read back from `_gate_block()`
    itself, not a parallel format string, so the two can never drift."""
    gate_clause = target.WRITE_SURFACE.clauses[0]
    bin_dir_stub = target.Path("stub-bin")

    declared_markers = {e.begin_marker for e in gate_clause.entries if e.path == "pre-commit"}
    for gate in target._GATE_REGISTRY:
        rendered = target._gate_block(gate, bin_dir_stub)
        header_line = rendered[0]
        assert header_line in declared_markers


def test_file_path_entries_cover_post_sync_hook_filenames():
    file_clause = target.WRITE_SURFACE.clauses[1]
    assert isinstance(file_clause, StaticClause)
    declared_paths = {entry.path for entry in file_clause.entries}
    assert declared_paths == set(target._POST_SYNC_HOOK_FILENAMES)
    for entry in file_clause.entries:
        assert entry.kind == "file-path"

