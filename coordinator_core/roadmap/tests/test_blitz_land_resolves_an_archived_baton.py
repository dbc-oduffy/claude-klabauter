"""A wave's own XS close-out archives its baton; the landing must still find it.

The XS lane's correct close-out `git mv`s the baton record into
``archive/handoffs/<YYYY-MM>/``, which is exactly where `land_wave`'s live scan
does not look — so a well-behaved dispatch produced "no baton on disk carries
id ...", a refusal that reads as a missing record when the record is right there
and terminal.

Measured 2026-09-10, wave 0 of run 20260910T000000Z: two of eight dispatched
batons archived themselves and both refused, in a landing whose other entries
succeeded, so the refusal looked like data loss rather than success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import blitz_land


def _record(path: Path, *, deliverable_id: str, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'title: "a baton"\n'
        "kind: session-handoff\n"
        "status: open\n"
        f"deployment_state: {state}\n"
        f'deliverable_id: "{deliverable_id}"\n'
        "---\n\n# body\n",
        encoding="utf-8",
    )


def _wave(ident: str) -> dict:
    return {"ready": [{"batonId": ident, "route": "dispatch"}]}


def test_an_archived_baton_this_wave_names_is_resolved(tmp_path):
    _record(
        tmp_path / "archive" / "handoffs" / "2026-08" / "closed.md",
        deliverable_id="dlv-archived-me",
        state="shipped",
    )
    found = blitz_land._archived_records_this_wave_names(
        tmp_path, _wave("dlv-archived-me"), []
    )
    assert [r["path"] for r in found] == ["archive/handoffs/2026-08/closed.md"]


def test_nothing_is_read_when_every_id_already_resolved(tmp_path):
    """Lazy for the reason plan_gate's own archive leg is: the archive is ~3x the
    live tree, and the normal answer is that nothing was missing."""
    _record(
        tmp_path / "archive" / "handoffs" / "2026-08" / "closed.md",
        deliverable_id="dlv-archived-me",
        state="shipped",
    )
    live = [{"ids": ["dlv-live"], "path": "state/handoffs/live.md"}]
    assert blitz_land._archived_records_this_wave_names(tmp_path, _wave("dlv-live"), live) == []


def test_an_id_in_no_tree_at_all_still_resolves_to_nothing(tmp_path):
    """Absent stays absent — this widens resolution, it does not invent a record."""
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    assert blitz_land._archived_records_this_wave_names(tmp_path, _wave("dlv-nowhere"), []) == []


@pytest.mark.parametrize("key", ["ready", "pulled", "replan", "surfacedToPm"])
def test_every_verdict_lane_is_searched(tmp_path, key):
    """A pulled or replanned baton can be archived too; resolution is not the
    ready lane's private affordance."""
    _record(
        tmp_path / "archive" / "handoffs" / "2026-08" / "closed.md",
        deliverable_id="dlv-archived-me",
        state="shipped",
    )
    wave = {key: [{"batonId": "dlv-archived-me"}]}
    found = blitz_land._archived_records_this_wave_names(tmp_path, wave, [])
    assert len(found) == 1
