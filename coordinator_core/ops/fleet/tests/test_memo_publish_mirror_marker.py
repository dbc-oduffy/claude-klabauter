"""The never-inbox guard recognises a publish mirror by its root marker file,
under any directory name; the marker ships from the klabauter toplevel source
and is absent from this authoring repo's root."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet._memo_resolver import (
    PUBLISH_MIRROR_MARKER,
    never_inbox_mirror_refusal,
)

REPO = Path(__file__).resolve().parents[4]


def test_marker_refuses_receiver_under_any_name(tmp_path):
    root = tmp_path / "my-checkout"
    root.mkdir()
    assert never_inbox_mirror_refusal("x-em", root) is None
    (root / PUBLISH_MIRROR_MARKER).write_text("m\n")
    assert "publish mirror" in never_inbox_mirror_refusal("x-em", root)


def test_basename_fallback_still_refuses(tmp_path):
    root = tmp_path / "claude-klabauter"
    root.mkdir()
    assert never_inbox_mirror_refusal("x-em", root) is not None


def test_marker_ships_from_klabauter_toplevel_and_not_authoring_root():
    assert (REPO / "dist" / "klabauter-toplevel" / PUBLISH_MIRROR_MARKER).is_file()
    assert not (REPO / PUBLISH_MIRROR_MARKER).exists()
