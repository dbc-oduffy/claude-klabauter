"""
coordinator_core.ops.tests.test_coordinator_complete_entry_deliverable_id

sedge-18 AC3 — a newly-scaffolded completion entry carries a POPULATED
`deliverable_id:` field. `_write_entry`'s ninth parameter
(`coordinator_core/ops/coordinator_complete_entry.py`) is the ceremony-
internal writer for this stamp (see that module's `_write_entry` docstring
and `_resolve_governing_deliverable_id`). No prior test in this tree asserted
the field is actually POPULATED on a scaffolded entry — existing hits
elsewhere (plan-fixture and rollup-sentence tests) only exercise the
unrelated `deliverable_id:` frontmatter field on plan files, never this
ceremony's own write.

Spec backlink: archive/handoffs/2026-08/2026-08-06_170018_roadmap-sedge-18.md § AC3
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.coordinator_complete_entry import _write_entry


def test_write_entry_stamps_populated_deliverable_id(tmp_path: Path) -> None:
    """A freshly-scaffolded entry's `deliverable_id:` frontmatter is present,
    non-empty, and equal to the value `_write_entry` was called with — not
    merely key-present (a `null` render, the pre-fix behavior for every
    entry, would satisfy "key-present" but not "populated")."""
    entry_path = tmp_path / "entry.md"

    wrote = _write_entry(
        str(entry_path),
        "session-abc123",
        "bugfix",
        "my-chain-slug",
        False,
        "loe:\n  agent_dispatches: null\n  opus_dispatches: null\n  em_tokens: null\n  tshirt: null",
        "",
        "2026-08-13",
        "dlv-sedge-18",
    )

    assert wrote is True
    text = entry_path.read_text(encoding="utf-8")
    parsed = parse_frontmatter(text)
    fm = parsed.get("frontmatter") or {}

    assert "deliverable_id" in fm
    assert fm["deliverable_id"]
    assert str(fm["deliverable_id"]) == "dlv-sedge-18"
