"""provenance_gaps: read_set mirrors pages_changed, gaps are commit dates minus daily files, one git spawn."""
from __future__ import annotations


import pytest

from coordinator_core.architecture_survey import provenance_gaps as pg


def test_read_set_equals_pages_changed_and_nothing_else() -> None:
    report = {"pages_changed": ["b.md", "a.md"], "dead_paths": [{"page": "z.md"}], "systems_without_page": ["q"]}
    assert pg.read_set(report) == ["a.md", "b.md"]
    assert pg.read_set({}) == []
