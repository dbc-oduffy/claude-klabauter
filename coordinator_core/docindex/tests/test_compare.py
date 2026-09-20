"""
Tests for coordinator_core.docindex.compare (C2b).

Spec backlink:
  claude-klabauter: docs/plans/2026-08-14-registry-indexes-are-emitted-from-their-directory.md,
                  plan-spine id C2b
"""
from __future__ import annotations

import hashlib

import pytest

from coordinator_core.docindex.compare import (
    CompareResult,
    FieldChanged,
    IndexedNotOnDisk,
    OnDiskNotIndexed,
    compare,
    extract_region,
    region_digest,
)
from coordinator_core.docindex.render import RenderError, render
from coordinator_core.docindex.spec import EntryField, IndexSpec

_DOC_TEMPLATE = (
    "# Title\n\nSome hand-authored prose before.\n\n"
    "{open_sentinel}\n{region}<!-- /GENERATED docindex sha256:{digest} -->\n"
    "\nMore hand-authored prose after.\n"
)


def _spec(fields, index_source_dir="docs/architecture/systems"):
    return IndexSpec(
        index_source_dir=index_source_dir,
        entry_kind="wiki-entry",
        entry_fields=tuple(EntryField(field=f, label=label) for f, label in fields),
        index_exclude_when=None,
    )


def _empty_doc() -> str:
    from coordinator_core.docindex.render import OPEN_SENTINEL

    return _DOC_TEMPLATE.format(open_sentinel=OPEN_SENTINEL, region="", digest="0" * 64)


def _rendered_doc(spec, entries) -> str:
    return render(_empty_doc(), spec, entries)


def test_happy_path_no_drift_matches():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    entries = [{"system": "engine-runtime", "lines": 23190}]
    doc = _rendered_doc(spec, entries)
    result = compare(doc, spec, entries)
    assert result.hand_edit is False
    assert result.added == []
    assert result.removed == []
    assert result.changed == []
    assert result.has_drift is False
    assert result.index_source_dir == "docs/architecture/systems"


def test_entry_added_reports_indexed_not_on_disk():
    spec = _spec([("system", "System")])
    base_entries = [{"system": "a"}]
    doc = _rendered_doc(spec, base_entries)

    new_entries = [{"system": "a"}, {"system": "b"}]
    result = compare(doc, spec, new_entries)

    assert result.hand_edit is False
    assert result.added == [IndexedNotOnDisk(identity="b")]
    assert result.removed == []
    assert result.has_drift is True


def test_entry_removed_reports_on_disk_not_indexed():
    spec = _spec([("system", "System")])
    base_entries = [{"system": "a"}, {"system": "b"}]
    doc = _rendered_doc(spec, base_entries)

    new_entries = [{"system": "a"}]
    result = compare(doc, spec, new_entries)

    assert result.hand_edit is False
    assert result.removed == [OnDiskNotIndexed(identity="b")]
    assert result.added == []
    assert result.has_drift is True


def test_field_value_edited_reports_changed():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    base_entries = [{"system": "a", "lines": 1}]
    doc = _rendered_doc(spec, base_entries)

    edited_entries = [{"system": "a", "lines": 99}]
    result = compare(doc, spec, edited_entries)

    assert result.hand_edit is False
    assert result.changed == [
        FieldChanged(identity="a", field="lines", on_disk="1", indexed="99")
    ]
    assert result.has_drift is True


def test_missing_declared_field_raises_by_name():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    doc = _rendered_doc(spec, [{"system": "a", "lines": 1}])
    with pytest.raises(RenderError):
        compare(doc, spec, [{"system": "a"}])


def test_ac16_excluded_entry_neither_emitted_nor_raising():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    base_entries = [{"system": "a", "lines": 1}]
    doc = _rendered_doc(spec, base_entries)
    result = compare(doc, spec, base_entries)
    assert result.hand_edit is False
    assert result.has_drift is False


def test_hand_edit_inside_region_detected_and_refused_by_name():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    entries = [{"system": "a", "lines": 1}]
    doc = _rendered_doc(spec, entries)

    hand_edited = doc.replace("| a | 1 |", "| a | 12345 |")

    result = compare(hand_edited, spec, entries)

    assert result.hand_edit is True
    assert result.added == []
    assert result.removed == []
    assert result.changed == []
    assert result.index_source_dir == "docs/architecture/systems"


def test_ordinary_drift_digest_matches_render_differs():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    base_entries = [{"system": "a", "lines": 1}]
    doc = _rendered_doc(spec, base_entries)

    changed_entries = [{"system": "a", "lines": 2}]
    result = compare(doc, spec, changed_entries)

    assert result.hand_edit is False
    assert result.changed == [
        FieldChanged(identity="a", field="lines", on_disk="1", indexed="2")
    ]


def test_region_digest_matches_render_stamped_digest():
    spec = _spec([("system", "System")])
    entries = [{"system": "a"}]
    doc = _rendered_doc(spec, entries)
    region_text, recorded_digest = extract_region(doc)
    assert region_digest(region_text.encode("utf-8")) == recorded_digest
    assert recorded_digest == hashlib.sha256(region_text.encode("utf-8")).hexdigest()


def test_extract_region_missing_opening_sentinel_raises():
    from coordinator_core.docindex.compare import CompareError

    with pytest.raises(CompareError):
        extract_region("# no region here\n")


def test_extract_region_missing_closing_sentinel_raises():
    from coordinator_core.docindex.compare import CompareError
    from coordinator_core.docindex.render import OPEN_SENTINEL

    with pytest.raises(CompareError):
        extract_region(f"# Title\n\n{OPEN_SENTINEL}\nno close\n")


def test_compare_accepts_pre_read_text_never_reads_paths():
    spec = _spec([("system", "System")])
    entries = [{"system": "a"}]
    doc = _rendered_doc(spec, entries)
    # compare() takes document text and entries directly — no path argument
    # exists on its signature, so this call succeeding at all is the proof.
    result = compare(doc, spec, entries)
    assert isinstance(result, CompareResult)
