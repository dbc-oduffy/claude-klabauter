"""Ratchet at zero: no guard returns a legacy envelope dict.

Every speaking corpus row fires with ``dispatch._render_record`` patched to hand its
record back unrendered, so each capture's envelope is the guard's raw result. A guard
whose raw result is a dict is legacy; only ``dispatch._PINNED_DICT_RETURNS`` may be.
"""
from __future__ import annotations

from typing import List

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_ROWS,
    EM_AUDIENCE,
    PLATFORM_CONDITIONED_ROWS,
    fire_row_for_audience,
)


def _legacy_dict_guards() -> List[str]:
    pinned = set(dispatch._PINNED_DICT_RETURNS)
    legacy = set()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COORDINATOR_CAP_FLEET_PRESENT", "1")
        mp.setattr(dispatch, "_render_record", lambda record, payload: record)
        for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
            if not row.expected_speaker or row.guard in pinned:
                continue
            if isinstance(fire_row_for_audience(row, EM_AUDIENCE).envelope, dict):
                legacy.add(row.guard)
    return sorted(legacy)


def test_no_guard_returns_a_bare_envelope_dict():
    legacy = _legacy_dict_guards()
    assert not legacy, "guards still returning a legacy envelope dict: %s" % legacy
