"""The removal side is enabled. The live-source refusal itself is pinned in
coordinator/lib/percolate/tests/test_removal_live_source_refusal.py.

AC6 history: the removal side refuses to delete a path that exists on disk.

Condition of assent from claude-central-em (2026-08-26) before the removal side
may be opened against a mirror this repo does not own, in their words "in the
code, not in the procedure".

AC2 fixes the CAUSE of the known false-positive class (`declared_payload`
sourced from the percolation SCAN surface misses a published-but-never-scanned
file, which then reads as undeclared). This pins the RECURRENCE catch. Both
known witnesses are fixtures here, because the whole argument for AC6 is that
the class has members nobody has enumerated yet:

  .github/scripts/check-persona-names.py   both mirrors; excluded from the
                                           transform sweep so the release-CI
                                           identity checker never scrubs itself
  coordinator_core/warm/door/door.exe      a binary in a DECLARED directory,
                                           tracked at HEAD, never scanned

No test here runs a real round or touches a publish mirror.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "percolate-round.py"
_spec = importlib.util.spec_from_file_location("percolate_round_ac6", _MOD_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_removal_side_is_enabled(tmp_path):
    assert _mod._REMOVAL_SIDE_ENABLED is True
