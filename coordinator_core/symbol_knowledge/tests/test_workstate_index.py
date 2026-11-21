"""Tests for coordinator_core.symbol_knowledge.workstate_index (fixture repos under tmp_path)."""

from __future__ import annotations


import pytest

from coordinator_core.symbol_knowledge import workstate_index as ws

SCIP = "scip-python python fixture 0.1 `pkg.mod`/fn()."


def test_extract_citations_ignores_fenced_blocks_for_symbols_and_tracks_sections() -> None:
    text = "# A\n\n## Sec One\n\nsee `a.b_c()` and x/y.py:3\n\n```\n`ignored.name` z/w.py\n```\n"
    refs = ws.extract_citations(text)
    assert ["s", "a.b_c", "sec-one"] in refs
    assert ["p", "x/y.py", "sec-one", 3, None] in refs
    assert not any(r[1] == "ignored.name" for r in refs)
