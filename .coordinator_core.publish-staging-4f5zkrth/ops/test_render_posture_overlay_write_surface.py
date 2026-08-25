"""Tests for `render_posture_overlay.WRITE_SURFACE`.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C3d (`render-posture-overlay`).

Purpose: proves the declared `rc-block` entry's marker pair byte-matches
`MARKER_START`/`MARKER_END` — the constants `_swap()` actually reads at
write time — rather than a retyped copy that could silently drift from
what the mechanism does. Also pins the `kind` choice (`rc-block`, not
`hook-gate-region`) and the unresolved `${_EM_CONTEXT_REPO_ROOT}` shape of
`path`.

Negative spec — this module does NOT:
  - exercise the insert/swap merge on disk (covered by this writer's own
    `test_render_posture_overlay.py`, out of this chunk's scope);
  - validate `WRITE_SURFACE` against `write_surface.validate()` (C4's
    emission-op concern, not this writer's);
  - declare or assert a second call site for `repo-setup` — `install.md:540`
    names it as a future caller, not a wired one today.
"""
from __future__ import annotations

from coordinator_core.install.write_surface import StaticClause
from coordinator_core.ops import render_posture_overlay as target


def test_write_surface_identity():
    declaration = target.WRITE_SURFACE
    assert declaration.writer_id == "render-posture-overlay"
    assert declaration.source_module == "coordinator_core.ops.render_posture_overlay"
    assert len(declaration.clauses) == 1


def test_rc_block_entry_derived_from_module_marker_constants():
    clause = target.WRITE_SURFACE.clauses[0]
    assert isinstance(clause, StaticClause)
    assert len(clause.entries) == 1

    entry = clause.entries[0]
    assert entry.kind == "rc-block"
    assert entry.begin_marker == target.MARKER_START
    assert entry.end_marker == target.MARKER_END
    assert entry.path == "${_EM_CONTEXT_REPO_ROOT}/.claude/em-context.md"


def test_end_marker_is_a_genuine_string_not_the_legacy_sentinel():
    """This writer has a real end marker on both sides — not the
    BEGIN-only `ABSENT_ON_LEGACY_INSTALLS` shape (that belongs to C6's two
    BEGIN-only writers, a different case entirely)."""
    entry = target.WRITE_SURFACE.clauses[0].entries[0]
    assert entry.end_marker is not None
    assert entry.end_marker != "absent-on-legacy-installs"


def test_anchor_enum_is_closed_at_three_values():
    assert target._VALID_ANCHORS == ("precision", "default", "substrate-free")
