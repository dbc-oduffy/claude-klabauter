"""
coordinator_core.cartography.tests.test_atlas_coverage_guard — standing
coverage invariant for the recorded architecture atlas.

Purpose: `docs/architecture/file-index.md` has no cheap regenerator — its
mapping rule and package table are hand-maintained prose, expanded over the
live tree by `atlas_record.expand_recorded_mapping`. Nothing previously
asserted on that expansion, so the document could silently rot (fall behind
new packages/files) while still reading as valid, parseable prose. This test
is that assertion: it is the thing standing between the atlas and silent
rot, not a fixture exercise of the parser.

Spec backlink: docs/architecture/file-index.md (the recorded mapping rule
and `## Directory → system` package table this test holds to account).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.cartography.atlas_record import (
    RecordedAtlas,
    expand_recorded_mapping,
    load_recorded_atlas,
)
from coordinator_core.cartography.tree import list_tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_recorded_atlas_classifies_every_tracked_source_file() -> None:
    """The recorded atlas must catalogue every tracked source file — zero
    uncatalogued paths under the live tree."""
    atlas: RecordedAtlas = load_recorded_atlas(REPO_ROOT)
    assert atlas.error is None, atlas.error_detail

    tracked = list_tracked_files(REPO_ROOT)
    expansion = expand_recorded_mapping(tracked, atlas)

    if expansion.uncatalogued:
        preview = "\n".join(f"  - {path}" for path in expansion.uncatalogued[:20])
        remaining = len(expansion.uncatalogued) - 20
        more = f"\n  ... and {remaining} more" if remaining > 0 else ""
        raise AssertionError(
            f"{len(expansion.uncatalogued)} tracked source file(s) are not "
            f"classified by the recorded atlas (docs/architecture/file-index.md):\n"
            f"{preview}{more}\n"
            "Fix: usually add a row to the package table under the relevant "
            "'## Directory → system' section (a new package appeared); only rarely "
            "does the fix belong in the 12-rule table instead."
        )
