"""Unit test — ``_validate_review_trail_file`` tolerates a ``-N`` uniqueness suffix.

Pins the claim made (but not previously tested) in ``review_trail_write.py``'s
``_reserve_unique_trail_path`` docstring: a same-second collision filename like
``2026-07-27-140000-abc12345-2.json`` must parse identically to the unsuffixed
form, because ``_TIME_SEG_RE`` only anchors on the leading digit run and treats
everything after the first ``-`` (including a ``-2`` uniqueness suffix) as an
opaque, unconsumed capture group.

Spec backlink: coordinator_core/ops/review_trail_write.py § _reserve_unique_trail_path
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.emit.sections._shared import _validate_review_trail_file


def _write_record(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "sha_range": "aaaa..bbbb",
                "reviewer": "code-reviewer",
                "verdict": "ok",
                "diff_loc": 42,
                "workstream": None,
            }
        ),
        encoding="utf-8",
    )


class TestValidateReviewTrailFileSuffixedFilename:
    """A ``-2``/``-3``-suffixed filename must round-trip identically to the bare form."""

    def test_suffixed_filename_parses_same_reviewed_at_as_bare(self, tmp_path: Path) -> None:
        bare = tmp_path / "2026-07-27-140000-abc12345.json"
        suffixed = tmp_path / "2026-07-27-140000-abc12345-2.json"
        _write_record(bare)
        _write_record(suffixed)

        bare_record, bare_reason = _validate_review_trail_file(str(bare))
        suffixed_record, suffixed_reason = _validate_review_trail_file(str(suffixed))

        assert bare_reason is None, f"unexpected quarantine of bare filename: {bare_reason}"
        assert suffixed_reason is None, (
            f"suffixed filename was quarantined — the -2 suffix broke parsing: {suffixed_reason}"
        )
        assert bare_record["reviewed_at"] == suffixed_record["reviewed_at"] == "2026-07-27T14:00:00Z"
        assert suffixed_record["sha_range"] == "aaaa..bbbb"
        assert suffixed_record["verdict"] == "ok"

    def test_double_digit_suffix_still_parses(self, tmp_path: Path) -> None:
        """A double-digit suffix (e.g. -10) is exactly as opaque to _TIME_SEG_RE as -2."""
        suffixed = tmp_path / "2026-07-27-140000-abc12345-10.json"
        _write_record(suffixed)

        record, reason = _validate_review_trail_file(str(suffixed))

        assert reason is None, f"unexpected quarantine: {reason}"
        assert record["reviewed_at"] == "2026-07-27T14:00:00Z"
