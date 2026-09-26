from __future__ import annotations

import hashlib
import re

import pytest

from coordinator_core.docindex.render import OPEN_SENTINEL, RenderError, render
from coordinator_core.docindex.spec import EntryField, IndexSpec

_CLOSE_RE = re.compile(r"<!-- /GENERATED docindex sha256:([0-9a-f]{64}) -->")


def _spec(fields):
    return IndexSpec(
        index_source_dir="docs/architecture/systems",
        entry_kind="wiki-entry",
        entry_fields=tuple(EntryField(field=f, label=label) for f, label in fields),
        index_exclude_when=None,
    )


def _doc(region: str = "") -> str:
    return (
        "# Title\n\nSome hand-authored prose before.\n\n"
        f"{OPEN_SENTINEL}\n{region}<!-- /GENERATED docindex sha256:{'0' * 64} -->\n"
        "\nMore hand-authored prose after.\n"
    )


def _extract_region_and_digest(text: str):
    open_idx = text.index(OPEN_SENTINEL) + len(OPEN_SENTINEL) + 1
    m = _CLOSE_RE.search(text)
    return text[open_idx : m.start()], m.group(1)


def test_happy_path_and_digest_stamped_outside_digested_span():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    entries = [{"system": "engine-runtime", "lines": 23190}]
    out = render(_doc(), spec, entries)
    region, digest = _extract_region_and_digest(out)
    assert digest == hashlib.sha256(region.encode("utf-8")).hexdigest()
    assert "| System | Lines |" in region
    assert "|---|---|" in region
    assert "| engine-runtime | 23,190 |" in region


def test_prose_outside_region_preserved_byte_identical():
    spec = _spec([("system", "System")])
    entries = [{"system": "engine-runtime"}]
    doc = _doc()
    out = render(doc, spec, entries)
    before = doc.split(OPEN_SENTINEL)[0]
    after = doc.split("More hand-authored prose after.")[1]
    assert out.startswith(before)
    assert out.endswith("More hand-authored prose after." + after)


def test_determinism_same_input_twice_same_bytes():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    entries = [
        {"system": "b-system", "lines": 10},
        {"system": "a-system", "lines": 5},
    ]
    out1 = render(_doc(), spec, entries)
    out2 = render(_doc(), spec, entries)
    assert out1 == out2


def test_sorted_by_identity_field():
    spec = _spec([("system", "System")])
    entries = [{"system": "zeta"}, {"system": "alpha"}, {"system": "mid"}]
    out = render(_doc(), spec, entries)
    region, _ = _extract_region_and_digest(out)
    rows = [line for line in region.splitlines() if line.startswith("| ") and "---" not in line]
    assert rows[1:] == ["| alpha |", "| mid |", "| zeta |"]


def test_entry_added_and_removed_and_field_value_edited():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    base_entries = [{"system": "a", "lines": 1}, {"system": "b", "lines": 2}]
    out1 = render(_doc(), spec, base_entries)

    added = base_entries + [{"system": "c", "lines": 3}]
    out2 = render(_doc(), spec, added)
    region2, _ = _extract_region_and_digest(out2)
    assert "| c | 3 |" in region2

    removed = [base_entries[0]]
    out3 = render(_doc(), spec, removed)
    region3, _ = _extract_region_and_digest(out3)
    assert "| b | 2 |" not in region3

    edited = [{"system": "a", "lines": 99}, {"system": "b", "lines": 2}]
    out4 = render(_doc(), spec, edited)
    region4, _ = _extract_region_and_digest(out4)
    assert "| a | 99 |" in region4
    assert out1 != out4


def test_outside_region_preservation_across_re_emit():
    spec = _spec([("system", "System")])
    entries = [{"system": "a"}]
    first = render(_doc(), spec, entries)
    second = render(first, spec, [{"system": "b"}])
    before = _doc().split(OPEN_SENTINEL)[0]
    after_marker = "More hand-authored prose after."
    assert second.startswith(before)
    assert second.split(after_marker)[1] == _doc().split(after_marker)[1]


def test_header_row_and_separator_use_declared_labels_inside_region():
    spec = _spec([("depends_on", "Dependencies"), ("entry_points", "Entry Points")])
    entries = [{"depends_on": [], "entry_points": "x"}]
    out = render(_doc(), spec, entries)
    region, _ = _extract_region_and_digest(out)
    lines = region.splitlines()
    assert lines[0] == "| Dependencies | Entry Points |"
    assert lines[1] == "|---|---|"


def test_cell_rendering_contract_pipe_and_newline_escaped():
    spec = _spec([("system", "System"), ("entry_points", "Entry Points")])
    value = (
        "2 harness-hook seams (`PreToolUse:Bash`, `PreToolUse:Write|Edit`) + "
        "per-guard `check()`"
    )
    entries = [{"system": "guards", "entry_points": value}]
    out = render(_doc(), spec, entries)
    region, _ = _extract_region_and_digest(out)
    assert "PreToolUse:Write\\|Edit" in region
    assert "\n" not in region.split("guards")[1].split("\n")[0]


def test_cell_rendering_contract_empty_list_sentinel():
    spec = _spec([("system", "System"), ("depends_on", "Dependencies")])
    entries = [{"system": "cartography", "depends_on": []}]
    out = render(_doc(), spec, entries)
    region, _ = _extract_region_and_digest(out)
    assert "| cartography | — |" in region


def test_cell_rendering_contract_numeric_thousands_separator():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    entries = [{"system": "assemblers", "lines": 41403}]
    out = render(_doc(), spec, entries)
    region, _ = _extract_region_and_digest(out)
    assert "| assemblers | 41,403 |" in region


def test_missing_opening_sentinel_raises():
    spec = _spec([("system", "System")])
    with pytest.raises(RenderError):
        render("# no region here\n", spec, [{"system": "a"}])


def test_missing_closing_sentinel_raises():
    spec = _spec([("system", "System")])
    doc = f"# Title\n\n{OPEN_SENTINEL}\nno close\n"
    with pytest.raises(RenderError):
        render(doc, spec, [{"system": "a"}])


def test_entry_missing_declared_field_raises():
    spec = _spec([("system", "System"), ("lines", "Lines")])
    with pytest.raises(RenderError):
        render(_doc(), spec, [{"system": "a"}])
