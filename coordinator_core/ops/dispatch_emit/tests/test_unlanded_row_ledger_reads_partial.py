"""A ledger item served only by Row-Unlanded chunks reads partial, in the verdict and the note's table."""
from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl

_SERVED = {"C1": ("R2",), "C2": ("R3",), "C3": ("R2", "R3")}
_VERDICT = {"items": [
    {"id": "R1", "status": "met"},
    {"id": "R2", "status": "met", "remaining": []},
    {"id": "R3", "status": "met"},
    {"id": "R4", "status": "unmet", "remaining": ["x"]},
]}
_UNLANDED = [("C1", "tests/test_addon_compat.py", "withheld by ask-1")]
_INLINE = {"criterion": {"status": "met"}}


def _status(verdict):
    return {i["id"]: i["status"] for i in verdict["items"]}


def test_item_served_only_by_an_unlanded_chunk_reads_partial_with_the_path():
    got = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, {"C1": ("R2",), "C2": ("R3",)})
    assert _status(got) == {"R1": "met", "R2": "partial", "R3": "met", "R4": "unmet"}
    assert got["items"][1]["remaining"] == ["tests/test_addon_compat.py"]
    assert _VERDICT["items"][1]["status"] == "met"


def test_item_with_a_landed_server_stays_met():
    got = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, _SERVED)
    assert _status(got)["R2"] == "met"
    assert got is _VERDICT


def test_claimed_by_counts_as_a_server():
    verdict = {"items": [{"id": "R9", "status": "met", "claimed_by": ["C1"]}]}
    got = tcl.downgrade_unlanded_items(verdict, _UNLANDED, {})
    assert got["items"][0]["status"] == "partial"


def test_no_unlanded_row_leaves_the_verdict_untouched():
    assert tcl.downgrade_unlanded_items(_VERDICT, [], _SERVED) is _VERDICT


def test_note_table_shows_partial_and_never_gets_a_second_section(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Delivery\n\nbody\n", encoding="utf-8")
    downgraded = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, {"C1": ("R2",)})
    tcl.rewrite_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    assert "| R2 | met |" in note.read_text(encoding="utf-8")
    tcl.rewrite_note_verdicts(note, downgraded, _INLINE, tmp_path)
    text = note.read_text(encoding="utf-8")
    assert text.count("## Verdicts") == 1
    assert "| R2 | partial |" in text and "| R2 | met |" not in text
    assert "tests/test_addon_compat.py" in text
    assert text.startswith("# Delivery\n\nbody\n")


def test_rewrite_keeps_a_section_that_follows_verdicts(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# D\n\n## Verdicts\n\nold\n\n## Appendix\n\nkeep\n", encoding="utf-8")
    tcl.rewrite_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    text = note.read_text(encoding="utf-8")
    assert "old" not in text and "## Appendix\n\nkeep" in text and text.count("## Verdicts") == 1


SELF = "5e1f0000-0000-4000-8000-0000000000c3"
RUN_A = "plan-20261021T100000-cccccc"
