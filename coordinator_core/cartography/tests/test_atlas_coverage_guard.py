
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
