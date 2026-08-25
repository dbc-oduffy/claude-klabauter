"""Guards for the mechanically-derived kill-ledger inventory.

The point of `kill_ledger_inventory` is that a roadmap built on it cannot quietly
omit a K-entry. These tests pin the three ways that guarantee could rot: a parser
that drops sections, a classifier that invents an UNCLASSIFIED bucket, and a
status-line substring test wide enough to misread authority prose as a status.
"""

from __future__ import annotations

import pytest

from coordinator_core.op_census import kill_ledger_inventory as kli


def _entry(status: str, *, key: str = "K-900", title: str = "a thing") -> str:
    return f"## {key} — {title}\n\n- **Status:** {status}\n\n**What is removed.** stuff.\n\n"


def test_parse_count_matches_heading_count() -> None:
    text = _entry("**LANDED**") + _entry("CANDIDATE — NOT YET CONVICTED", key="K-901")
    entries = kli.parse_ledger(text)
    assert [e.key for e in entries] == ["K-900", "K-901"]


def test_parser_drop_is_an_exception_not_a_silent_omission(tmp_path) -> None:
    ledger = tmp_path / "kill-ledger.md"
    ledger.write_text(_entry("**LANDED**"), encoding="utf-8")
    original = kli.parse_ledger
    try:
        kli.parse_ledger = lambda _text: []  # type: ignore[assignment]
        with pytest.raises(AssertionError, match="dropped a section"):
            kli.build(ledger)
    finally:
        kli.parse_ledger = original  # type: ignore[assignment]


def test_no_entry_is_left_unclassified() -> None:
    entries = kli.parse_ledger(_entry("something the rules have never seen"))
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"
    assert entries[0].notes  # a CONTESTED row always states why


def test_authority_prose_does_not_override_a_landed_status() -> None:
    """`closed out by C1g` is authority prose, not a CLOSED status — the K-012
    misclassification this window guards against."""
    entries = kli.parse_ledger(
        _entry("**LANDED** - **Date:** 2026-08-21 - **Authority:** plan F-1; chunks C1a-C1j, closed out by C1g")
    )
    kli.classify(entries, live_ops=frozenset(), suspended_ops=frozenset())
    assert entries[0].population == "LANDED"


def test_landed_status_on_a_live_op_is_contested() -> None:
    entries = kli.parse_ledger(_entry("removed", title="`hooks.example_op` (cut elsewhere)"))
    kli.classify(entries, live_ops=frozenset({"hooks.example_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CONTESTED"


def test_candidate_status_on_a_live_op_stays_a_candidate() -> None:
    entries = kli.parse_ledger(
        _entry("CANDIDATE — MEASURED ON WALL CLOCK, NOT YET CONVICTED", title="`fleet.example_op`")
    )
    kli.classify(entries, live_ops=frozenset({"fleet.example_op"}), suspended_ops=frozenset())
    assert entries[0].population == "CANDIDATE"


def test_a_file_path_is_not_read_as_an_op_name() -> None:
    entries = kli.parse_ledger(_entry("**LANDED**", title="`coverage.py`'s orphaned surface"))
    assert entries[0].op_name is None


def test_real_ledger_classifies_every_entry() -> None:
    entries, heading_count = kli.build()
    assert len(entries) == heading_count
    assert not [e for e in entries if e.population == "UNCLASSIFIED"]
