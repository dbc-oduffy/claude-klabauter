"""test_stitch_observer_sidecar.py — unit tests for
coordinator/bin/stitch-observer-sidecar.py.

Coverage (this file, added for the heading+placeholder corruption fix):
  - heading+placeholder mixed state, real section already present: idempotent
    path taken, stray placeholder stripped, exactly one heading survives,
    sidecar removed, exit 0.
  - post-write verification failure: daily summary is restored to its exact
    pre-write bytes, the sidecar is retained on disk, exit 1.
  - analyst-brief heading directly above the placeholder, sidecar content NOT
    yet in the file: that heading belongs to the placeholder (per
    `_strip_placeholder`'s adjacency rule), not a real section — must fall
    through to the normal append path rather than the idempotent path.
    Regression guard for a restructure that decided idempotent-vs-append on
    the heading count BEFORE placeholder-stripping, which misrouted this case
    into the marker-mismatch LEAK branch and returned 1 instead of stitching.

Module import: stitch-observer-sidecar.py is a hyphenated filename, loaded by
file path (same idiom as test_wsc_close.py in this same tests/ dir).

Spec backlink: cross-repo/inbox/2026-08-11-example-retrieval-repo-em-stitch-observer-
sidecar-corrupts-on-heading-plus-placeholder.md

Run:
    python -m pytest coordinator/bin/tests/test_stitch_observer_sidecar.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_stitch_module():
    spec = importlib.util.spec_from_file_location(
        "stitch_observer_sidecar_test_module", _BIN_DIR / "stitch-observer-sidecar.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_stitch = _load_stitch_module()

_SIDECAR_BODY = "## Strategic Review (Sonnet daily observer)\n\nObserver findings here.\n"


def test_heading_and_placeholder_takes_idempotent_path_and_strips_residue(tmp_path):
    daily = tmp_path / "2026-06-25.md"
    sidecar = tmp_path / "2026-06-25.machine-a.observer.md"

    daily.write_text(
        "# Daily Summary\n\n"
        "_Strategic Review section will be appended by the reviewer agent._\n\n"
        "## Strategic Review (Sonnet daily observer)\n\n"
        "Observer findings here.\n",
        encoding="utf-8",
    )
    sidecar.write_text(_SIDECAR_BODY, encoding="utf-8")

    rc = _stitch.stitch(str(daily), str(sidecar))

    assert rc == 0
    assert not sidecar.exists()
    final = daily.read_text(encoding="utf-8")
    assert _stitch._PLACEHOLDER not in final
    assert _stitch._heading_count(final) == 1


def test_verification_failure_restores_original_bytes_and_keeps_sidecar(tmp_path):
    daily = tmp_path / "2026-07-01.md"
    sidecar = tmp_path / "2026-07-01.machine-a.observer.md"

    original = "# Daily Summary\n\nSome analyst prose.\n"
    daily.write_text(original, encoding="utf-8")
    sidecar.write_text(_SIDECAR_BODY, encoding="utf-8")

    # Force the post-write verification to see 0 headings so the failure
    # branch fires without needing a genuinely malformed sidecar.
    with mock.patch.object(_stitch, "_heading_count", return_value=0):
        rc = _stitch.stitch(str(daily), str(sidecar))

    assert rc == 1
    assert sidecar.exists()
    assert daily.read_text(encoding="utf-8") == original


def test_placeholders_own_heading_falls_through_to_append(tmp_path):
    daily = tmp_path / "2026-07-15.md"
    sidecar = tmp_path / "2026-07-15.machine-a.observer.md"

    # The analyst brief emitted its own "## Strategic Review" heading
    # directly above the placeholder — that heading belongs to the
    # placeholder (per _strip_placeholder's adjacency rule), and the
    # sidecar's content is NOT anywhere else in the file. This must take the
    # normal append path, not the idempotent path.
    daily.write_text(
        "# Daily Summary\n\n"
        "## Strategic Review (Sonnet daily observer)\n\n"
        "_Strategic Review section will be appended by the reviewer agent._\n",
        encoding="utf-8",
    )
    sidecar.write_text(_SIDECAR_BODY, encoding="utf-8")

    rc = _stitch.stitch(str(daily), str(sidecar))

    assert rc == 0
    assert not sidecar.exists()
    final = daily.read_text(encoding="utf-8")
    assert _stitch._PLACEHOLDER not in final
    assert _stitch._heading_count(final) == 1
    assert "Observer findings here." in final
