#!/usr/bin/env python3
"""test_provenance.py — regression tests for the provenance resolver.

Covers `get_provenance_completeness`:
  T1   absent system key → 'unknown'
  T2   system present but no provenance_completeness key → 'unknown'
  T2b  provenance_completeness explicitly None → 'unknown'
  T2c  provenance_completeness empty string → 'unknown'
  T3   system={'provenance_completeness':'complete'} → 'complete'
  T4   system={'provenance_completeness':'unknown'} → 'unknown'
  T5   system not a dict (e.g. None) → 'unknown'
  T5b  system is a non-dict scalar → 'unknown'
  T6   unrecognized non-empty string passes through unchanged

Run: python3 -m pytest coordinator/bin/lib/test_provenance.py

Spec backlink: state/handoffs/2026-06-27_095003_roadmap-ccos-3.md § Specification
  (ccos-3 — shared read-side resolver)
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import the module under test from the same directory as this file. __file__-relative,
# so it resolves identically under a serial run and under an xdist worker, neither of
# which is guaranteed to carry the invocation cwd on sys.path.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from provenance import get_provenance_completeness  # noqa: E402

# These nine assertions lived in a hand-rolled `run_tests()` under an `if __name__ ==
# "__main__"` guard until 2026-07-28. Because the file was ALREADY named test_*.py, the
# 2026-07-25 collectability migration (3e818e6b) counted it among the "already
# collectable" files and never opened it — but pytest imported the module, found no
# `test_`-prefixed function, and collected zero tests. That reads as a clean module
# everywhere it matters: no collection error, no failure, and nothing in a tier summary
# distinguishes "module contributed 0 tests" from "module passed". Nor was anything
# running it the old way — no .sh/.cmd runner invoked this path, so all nine assertions
# were inert on every tier. Converted 1:1 to real test functions; assertion intent and
# the T-numbering are preserved exactly.


def test_absent_system_key_resolves_unknown() -> None:
    """T1: a record with no 'system' block resolves to the design-B default."""
    assert get_provenance_completeness({}) == "unknown"


def test_system_without_provenance_completeness_resolves_unknown() -> None:
    """T2: a 'system' block lacking the key resolves to the design-B default."""
    assert get_provenance_completeness({"system": {"other_field": "value"}}) == "unknown"


def test_provenance_completeness_none_resolves_unknown() -> None:
    """T2b: an explicitly-null value is absence, not a stored value."""
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": None}})
        == "unknown"
    )


def test_provenance_completeness_empty_string_resolves_unknown() -> None:
    """T2c: an empty string is absence, not a stored value."""
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": ""}})
        == "unknown"
    )


def test_complete_value_is_returned() -> None:
    """T3: a stored 'complete' is returned as-is."""
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": "complete"}})
        == "complete"
    )


def test_stored_unknown_value_is_returned() -> None:
    """T4: a stored 'unknown' is returned as-is."""
    assert (
        get_provenance_completeness({"system": {"provenance_completeness": "unknown"}})
        == "unknown"
    )


def test_system_none_resolves_unknown() -> None:
    """T5: a null 'system' block is not a dict and resolves to the default."""
    assert get_provenance_completeness({"system": None}) == "unknown"


def test_system_non_dict_scalar_resolves_unknown() -> None:
    """T5b: a scalar 'system' block is not a dict and resolves to the default."""
    assert get_provenance_completeness({"system": "not-a-dict"}) == "unknown"


def test_unrecognized_value_passes_through_unchanged() -> None:
    """T6: an invalid enum value is NOT normalised to 'unknown'.

    The resolver's docstring promises it "returns the stored value when present";
    silently folding a corrupted value into 'unknown' would hide the corruption.
    """
    # Review: code-reviewer F4 — asserts the documented pass-through contract.
    assert (
        get_provenance_completeness(
            {"system": {"provenance_completeness": "corrupted_value"}}
        )
        == "corrupted_value"
    )
