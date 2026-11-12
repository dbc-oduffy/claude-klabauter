"""Packing of under-filled batch_key remainder chunks in grind_compose._group_into_batches."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit.queue_select import Manifest, ManifestEntry

pytestmark = pytest.mark.complete

_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _profile():
    profile = gp.load_profile("fixture", _PROFILE_DIR)
    gp.validate_graph(profile)
    return profile


def _manifest(keys_to_counts):
    entries = []
    for key, count in keys_to_counts.items():
        for n in range(count):
            entries.append(
                ManifestEntry(row_id=f"{key}-r{n}", path=f"q/{key}-r{n}.yaml", digest=f"{len(entries):064x}", batch_key=key)
            )
    return Manifest(entries=tuple(entries), batch_sizes={}, source=None, digest="d")


def test_five_single_row_keys_pack_into_two_batches():
    manifest = _manifest({f"K{i}": 1 for i in range(5)})
    batches = gc._group_into_batches(manifest, {"batch_size": 4}, _profile())
    assert [len(rows) for _, rows in batches] == [4, 1]
    assert batches[0][0] == "@packed-b0"
    assert all(":" not in bid for bid, _ in batches)


def test_without_profile_every_key_is_its_own_batch():
    manifest = _manifest({f"K{i}": 1 for i in range(5)})
    assert len(gc._group_into_batches(manifest, {"batch_size": 4})) == 5


def test_full_chunks_unchanged_and_remainder_packed():
    manifest = _manifest({"A": 5, "B": 1, "C": 2})
    batches = dict(gc._group_into_batches(manifest, {"batch_size": 4}, _profile()))
    assert [e.row_id for e in batches["A-b0"]] == [f"A-r{n}" for n in range(4)]
    assert sorted(e.row_id for e in batches["@packed-b0"]) == ["A-r4", "B-r0", "C-r0", "C-r1"]


def test_lone_remainder_keeps_own_id():
    manifest = _manifest({"A": 1})
    assert [bid for bid, _ in gc._group_into_batches(manifest, {"batch_size": 4}, _profile())] == ["A-b0"]


def test_every_row_batched_exactly_once():
    manifest = _manifest({f"K{i}": i % 3 + 1 for i in range(9)})
    batches = gc._group_into_batches(manifest, {"batch_size": 4}, _profile())
    ids = [e.row_id for _, rows in batches for e in rows]
    assert sorted(ids) == sorted(e.row_id for e in manifest.entries)
    assert all(len(rows) <= 4 for _, rows in batches)


def test_unequal_verify_mode_is_not_packed():
    profile = _profile()
    verify = next(n for n in profile.graph.values() if n.kind == "verify").verify
    assert verify["P0"] != verify["default"]
    manifest = _manifest({"P0": 1, "K1": 1, "K2": 1})
    batches = gc._group_into_batches(manifest, {"batch_size": 4}, profile)
    by_id = {bid: sorted({e.batch_key for e in rows}) for bid, rows in batches}
    assert sorted(by_id.values()) == [["K1", "K2"], ["P0"]]


def test_unequal_triage_depth_is_not_packed():
    manifest = _manifest({"K0": 1, "K1": 1})
    knobs = {"batch_size": 4, "triage_depth": {"default": "standard", "K1": "deep"}}
    assert len(gc._group_into_batches(manifest, knobs, _profile())) == 2


def test_packed_script_rows_read_their_own_batch_key():
    profile = _profile()
    manifest = _manifest({"K0": 1, "K1": 1})
    knobs = gp.resolve_appetite(profile, "standard")
    script = gc.compose_grind_script(
        manifest, profile, knobs, run_dir=Path("state/queue-grind/fixture/run-1"), agent_type_host=None, **_review_kw()
    )
    assert "batchKey: _entry.batch_key" in script
    assert "@packed-b0" in script


def _review_kw():
    from .conftest import REVIEW_KW

    return REVIEW_KW
