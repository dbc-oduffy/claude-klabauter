"""Regression coverage for the C9 retarget (Ruling 4, 2026-08-21
rebuild-the-three-ceremony-assemblers plan): `workstream_complete`'s leg B
(`_dispatch_has_live_children`) stopped walking state/handoffs/ +
archive/handoffs/ + archive/completed/ to answer "does any live handoff name
me as predecessor", and instead reads a single write-time back-edge off the
candidate's own frontmatter — `continued_into`, stamped by THIS module's
`_dispatch_handoff_supersede_predecessor` (via its composed
`handoff.archive_transition` mode="supersede" call, `_supersede_continued`
being the one writer of that field).

No production code changed here — `continued_into` was already stamped by
`_dispatch_handoff_supersede_predecessor` before this plan, and already has
extensive coverage of ITS OWN in `coordinator_core/test_baton_assemble.py`
(`_ReplayHarness.continued_into()`). What this file pins, which nothing
before C9 needed to, is the CROSS-MODULE CONTRACT: that the exact field this
module stamps is the exact field `workstream_complete._dispatch_has_live_
children` now reads, end to end through a real `apply()` supersede run — not
two modules independently agreeing on a field name by convention, unverified.

A future change to either side (renaming the field here, or reading a
different key over there) breaks this file, not silently drifting the two
apart.
"""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.workstream_complete import _dispatch_has_live_children
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _PRED_REL,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _ReplayHarness,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def test_supersede_stamped_back_edge_is_read_by_wsc_leg_b_as_a_live_child(tmp_path, monkeypatch):
    harness = _ReplayHarness(tmp_path, monkeypatch)
    exit_code, report = harness.run()

    assert exit_code == 0
    successor = harness.continued_into()
    assert successor

    archived = harness.archived_predecessor()
    assert archived is not None
    archived_rel = archived.relative_to(harness.repo).as_posix()

    result = _dispatch_has_live_children(harness.repo, archived_rel)

    assert result["exit_code"] == 0
    assert result["referenced"] is True


def test_a_never_superseded_predecessor_reads_as_no_children(tmp_path, monkeypatch):
    harness = _ReplayHarness(tmp_path, monkeypatch)

    result = _dispatch_has_live_children(harness.repo, _PRED_REL)

    assert result["exit_code"] == 1
    assert result["referenced"] is False
