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
    _REPO_MAKIMA_BIN,
    _ReplayHarness,
)

# `_ReplayHarness` drives a REAL `apply()` run against a real git repo and the
# REAL `handoff.archive_transition` op — no mock stands in for the write this
# file pins. Mirrors the spawn/cadence marking of its sibling suites in this
# directory (`test_ledger_claim_record_liveness.py`,
# `test_apply_degrade_no_compensation.py`), which use the same harness.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Restated per-module, as every sibling suite using `_ReplayHarness`
    does — an autouse fixture is module-scoped and does not cross into this
    one. See `test_apply_degrade_no_compensation.py`'s identical fixture for
    why: without it, `resolve_operator_config()` fails loud under this
    suite's HOME quarantine and every `apply()` run aborts at d1."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_makima_bin", lambda: _REPO_MAKIMA_BIN)


def test_supersede_stamped_back_edge_is_read_by_wsc_leg_b_as_a_live_child(tmp_path, monkeypatch):
    """The end-to-end contract: run a real `apply()` supersede (d1 mints a
    successor, d6 supersedes+archives the predecessor), then hand the
    archived predecessor's own repo-relative path to `workstream_complete
    ._dispatch_has_live_children` exactly as `_evaluate_consumed_handoff_
    completeness_element` does — and confirm it reports a live child, off
    the SAME `continued_into` field d6 just stamped, with no op dispatch,
    no IPC hop, no corpus walk."""
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
    """The negative case, over the SAME harness fixture, run only through
    seeding (no `apply()` call): a predecessor carrying no `continued_into`
    at all reads as leg B's genuine "no-children" verdict, `exit_code=1` —
    not a fallback scan, because none exists anymore."""
    harness = _ReplayHarness(tmp_path, monkeypatch)

    result = _dispatch_has_live_children(harness.repo, _PRED_REL)

    assert result["exit_code"] == 1
    assert result["referenced"] is False
