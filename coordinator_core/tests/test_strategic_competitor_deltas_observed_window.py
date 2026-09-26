"""
Tests for Item 28: derive_competitor_deltas builds a time axis from observed_window.

Fixture shape follows the wire contract in
state/cross-repo/archive/2026-09-24-example-market-data-repo-em-x-observed-window-reply-klabauter-claude-klabauter.md:
a competitor entry may carry `observed_window: {"from": ISO-8601, "to": ISO-8601}` plus a
sibling `observed_window_basis` (accepted but not read for ordering).
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.strategic.competitor_deltas import derive_competitor_deltas

REPO_ROOT = Path("/tmp/unused-repo-root")


def _window(from_ts: str, to_ts: str) -> dict:
    return {"from": from_ts, "to": to_ts, "observed_window_basis": "publication_date"}


def test_added_competitors_order_ascending_by_observed_window_from():
    prev_snapshot = {"competitors": []}
    snapshot = {
        "competitors": [
            {"name": "Later Co", "observed_window": _window("2026-03-01T00:00:00Z", "2026-03-05T00:00:00Z")},
            {"name": "Earlier Co", "observed_window": _window("2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z")},
        ]
    }

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert [d["name"] for d in deltas] == ["Earlier Co", "Later Co"]


def test_entries_without_observed_window_fall_back_to_name_order():
    prev_snapshot = {"competitors": []}
    snapshot = {
        "competitors": [
            {"name": "Zeta Co"},
            {"name": "Alpha Co"},
        ]
    }

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert [d["name"] for d in deltas] == ["Alpha Co", "Zeta Co"]


def test_windowed_entries_sort_ahead_of_unwindowed_entries():
    prev_snapshot = {"competitors": []}
    snapshot = {
        "competitors": [
            {"name": "No Window Co"},
            {"name": "Windowed Co", "observed_window": _window("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")},
        ]
    }

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert [d["name"] for d in deltas] == ["Windowed Co", "No Window Co"]


def test_absent_observed_window_is_present_as_null_not_an_error():
    prev_snapshot = {"competitors": []}
    snapshot = {
        "competitors": [
            {"name": "Malformed Window Co", "observed_window": "not-a-dict"},
            {"name": "Null Window Co", "observed_window": None},
        ]
    }

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert [d["name"] for d in deltas] == ["Malformed Window Co", "Null Window Co"]


def test_removed_competitors_also_order_by_prev_snapshot_observed_window():
    prev_snapshot = {
        "competitors": [
            {"name": "Later Removed", "observed_window": _window("2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z")},
            {"name": "Earlier Removed", "observed_window": _window("2026-02-01T00:00:00Z", "2026-02-02T00:00:00Z")},
        ]
    }
    snapshot = {"competitors": []}

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert [d["name"] for d in deltas] == ["Earlier Removed", "Later Removed"]


def test_emitted_shape_is_unchanged_by_the_time_axis():
    prev_snapshot = {"competitors": []}
    snapshot = {
        "competitors": [
            {"name": "Windowed Co", "observed_window": _window("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")},
        ]
    }

    deltas = derive_competitor_deltas(REPO_ROOT, snapshot, prev_snapshot)

    assert deltas == [
        {
            "name": "Windowed Co",
            "note": "New competitor observed since prior snapshot.",
            "provenance": "generated",
        }
    ]


def test_none_snapshot_still_degrades_to_empty_list():
    assert derive_competitor_deltas(REPO_ROOT, None, {"competitors": []}) == []
    assert derive_competitor_deltas(REPO_ROOT, {"competitors": []}, None) == []
