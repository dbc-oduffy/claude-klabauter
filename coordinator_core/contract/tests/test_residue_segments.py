"""
coordinator_core.contract.tests.test_residue_segments — pytest for the
shared, caller-agnostic segment loader factored out of
`coordinator_core.review_assemble.residue`.

Deliberately exercises the loader with a filter key OTHER than `surface`
(`case`, mirroring `/handoff`'s own frontmatter shape) — proving the
parameterisation is real, not re-testing review's own values (that stays
`coordinator_core/review_assemble/test_residue.py`'s job, unedited, as the
behaviour-preservation oracle).

Run: python -m pytest coordinator_core/contract/tests/test_residue_segments.py -q

Spec backlink: pln-factor-the-residue-segment-loa-e63300, chunk C1
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.contract.residue_segments import (
    SEGMENT_CLASSES,
    SegmentLoadError,
    load_segments,
    select_segments,
)

SEGMENT_DIR = "skills/handoff/residue"
LEGAL_CASES = ("compaction", "wrap", "shared")


def _segment_text(segment_id: str, case: str, cls: str, order: int, body: str) -> str:
    return (
        "---\n"
        f"segment_id: {segment_id}\n"
        f"case: {case}\n"
        f"class: {cls}\n"
        f"order: {order}\n"
        "---\n"
        f"{body}\n"
    )


def _make_segment_dir(tmp_path: Path) -> Path:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-shared.md").write_text(
        _segment_text("shared-reminder", "shared", "protected", 0, "Shared body."),
        encoding="utf-8",
    )
    (segment_dir / "020-compaction.md").write_text(
        _segment_text("compaction-reminder", "compaction", "droppable", 1, "Compaction body."),
        encoding="utf-8",
    )
    (segment_dir / "030-wrap.md").write_text(
        _segment_text("wrap-reminder", "wrap", "droppable", 2, "Wrap body."),
        encoding="utf-8",
    )
    return content_root


# ---------------------------------------------------------------------------
# Happy path — parameterised on a filter key other than `surface`.
# ---------------------------------------------------------------------------


def test_load_and_select_round_trip_with_non_surface_filter_key(tmp_path: Path) -> None:
    content_root = _make_segment_dir(tmp_path)
    segments = load_segments(
        content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES
    )
    assert {s["segment_id"] for s in segments} == {
        "shared-reminder",
        "compaction-reminder",
        "wrap-reminder",
    }

    selected = select_segments(
        segments, filter_key="case", active_values={"compaction", "shared"}
    )
    assert [s["segment_id"] for s in selected] == ["shared-reminder", "compaction-reminder"]


def test_select_segments_sorts_by_order_ascending(tmp_path: Path) -> None:
    content_root = _make_segment_dir(tmp_path)
    segments = load_segments(
        content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES
    )
    selected = select_segments(
        segments, filter_key="case", active_values={"compaction", "wrap", "shared"}
    )
    assert [s["order"] for s in selected] == sorted(s["order"] for s in selected)


def test_source_path_is_relative_to_content_root_not_absolute(tmp_path: Path) -> None:
    content_root = _make_segment_dir(tmp_path)
    segments = load_segments(
        content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES
    )
    content_root_str = str(content_root)
    for segment in segments:
        source_path = segment["source_path"]
        assert not Path(source_path).is_absolute(), source_path
        assert content_root_str not in source_path, source_path
        assert source_path.startswith(f"{SEGMENT_DIR}/"), source_path


def test_loaded_segment_carries_filter_key_and_class(tmp_path: Path) -> None:
    content_root = _make_segment_dir(tmp_path)
    segments = load_segments(
        content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES
    )
    by_id = {s["segment_id"]: s for s in segments}
    assert by_id["compaction-reminder"]["case"] == "compaction"
    assert by_id["compaction-reminder"]["class"] in SEGMENT_CLASSES


# ---------------------------------------------------------------------------
# Fail-loud coverage — AC3's checklist, no silent skip on any of them.
# ---------------------------------------------------------------------------


def test_missing_segment_directory_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"  # never created
    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "residue directory not found" in str(excinfo.value)


def test_empty_segment_directory_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    (content_root / SEGMENT_DIR).mkdir(parents=True)
    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "no segment files" in str(excinfo.value)


def test_unparseable_frontmatter_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-broken.md").write_text("no frontmatter here\n", encoding="utf-8")

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "no parseable frontmatter" in str(excinfo.value)


def test_missing_required_field_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-incomplete.md").write_text(
        "---\nsegment_id: incomplete\ncase: shared\nclass: protected\n---\nBody.\n",
        encoding="utf-8",
    )

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "missing required frontmatter field(s)" in str(excinfo.value)
    assert "order" in str(excinfo.value)


def test_out_of_enum_class_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-bad-class.md").write_text(
        _segment_text("bad-class", "shared", "bogus", 0, "Body."),
        encoding="utf-8",
    )

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "class='bogus'" in str(excinfo.value) or "class=\"bogus\"" in str(excinfo.value)


def test_out_of_enum_filter_value_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-bad-case.md").write_text(
        _segment_text("bad-case", "bogus-case", "protected", 0, "Body."),
        encoding="utf-8",
    )

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "bogus-case" in str(excinfo.value)
    assert str(LEGAL_CASES) in str(excinfo.value)


def test_non_integer_order_is_fail_loud(tmp_path: Path) -> None:
    content_root = tmp_path / "content-root"
    segment_dir = content_root / SEGMENT_DIR
    segment_dir.mkdir(parents=True)
    (segment_dir / "010-bad-order.md").write_text(
        "---\nsegment_id: bad-order\ncase: shared\nclass: protected\norder: not-a-number\n"
        "---\nBody.\n",
        encoding="utf-8",
    )

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "is not an integer" in str(excinfo.value)


def test_unreadable_segment_file_is_fail_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content_root = _make_segment_dir(tmp_path)

    from coordinator_core.contract import residue_segments as residue_segments_mod

    real_read_text = Path.read_text

    def _boom(self: Path, *args, **kwargs):
        if self.name == "010-shared.md":
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(residue_segments_mod.Path, "read_text", _boom)

    with pytest.raises(SegmentLoadError) as excinfo:
        load_segments(content_root, SEGMENT_DIR, filter_key="case", legal_values=LEGAL_CASES)
    assert "could not read" in str(excinfo.value)
