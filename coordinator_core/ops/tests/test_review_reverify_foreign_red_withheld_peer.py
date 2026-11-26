"""`review.reverify` classes a pathless red `foreign_red`, naming the holding run in `foreign_red_runs`,
when its test file reaches a path another halted run withholds and reaches none of this run's paths.

Trap: `halted_withheld_index.withheld_by_path` and `land_run.foreign_halted_withheld` are stubbed here, so
record parsing is pinned by their own tests, not by this file."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import halted_withheld_index, land_run
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

HELD = "peerpkg/held.py"
PEER_RUN = "ask-20261124T233719-fa2dc6"
NODE = "tests/test_peer_reach.py::test_slice_extracts"
PATHLESS = "ReferenceError: _emptyStageResume is not defined"


def _peer(root, monkeypatch, *, test_src="from peerpkg import held\n", record=PEER_RUN, index=(HELD,)):
    """A test file outside the footprint reaching `peerpkg/held.py`, which a halted peer run withholds."""
    for rel, text in {
        "peerpkg/__init__.py": "",
        HELD: "let = 1\n",
        "tests/test_peer_reach.py": test_src + "\n\ndef test_slice_extracts():\n    assert held\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))
    monkeypatch.setattr(halted_withheld_index, "withheld_by_path", lambda _r: {p: PEER_RUN for p in index})
    asked: list = []

    def held_by(_r, _id, _p, paths):
        asked.append(list(paths))
        return {p: record for p in paths if p in index}

    monkeypatch.setattr(land_run, "foreign_halted_withheld", held_by)
    return asked


def _run(root):
    # failing_at_base=[] marks the node green at base, so the unreachable-red route cannot class it.
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[NODE], failing_at_base=[],
        run_id="me", failing_output={NODE: PATHLESS},
    )


def test_pathless_red_reaching_a_halted_peers_withheld_path_is_foreign_red_naming_the_run(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch)
    out = _run(root)
    assert out["foreign_red"] == {NODE: [HELD]}
    assert out["foreign_red_runs"] == {NODE: [PEER_RUN]}
    assert out["unattributed"] == [] and out["sibling_owned"] == []
    assert from_reverify({**out, "failing": [NODE]}) == []


def test_a_literal_naming_the_withheld_path_reaches_it(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch, test_src=f"SRC = {HELD!r}\nheld = SRC\n")
    out = _run(root)
    assert out["foreign_red_runs"] == {NODE: [PEER_RUN]}


def test_a_withheld_path_this_run_edited_stays_a_blocker(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch, test_src="from pkg import mod as held\n", index=("pkg/mod.py",))
    out = _run(root)
    assert out["foreign_red"] == {} and out["foreign_red_runs"] == {}
    assert out["unattributed"] == [NODE]


def test_a_test_file_also_reaching_a_footprint_path_stays_a_blocker(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch, test_src="from pkg import mod\nfrom peerpkg import held\n")
    out = _run(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_a_failure_text_naming_a_footprint_path_stays_a_blocker(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch)
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[NODE], failing_at_base=[],
        run_id="me", failing_output={NODE: PATHLESS + "\n    at pkg/mod.py:1"},
    )
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_no_withheld_path_anywhere_blocks_without_asking_the_halt_records(world, monkeypatch) -> None:
    root, _orig, _declared = world
    asked = _peer(root, monkeypatch, index=())
    out = _run(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]
    assert asked == []


def test_a_withheld_path_the_reach_never_touches_blocks(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch, index=("elsewhere/other.py",))
    out = _run(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_an_unreadable_halt_record_stays_a_blocker(world, monkeypatch) -> None:
    root, _orig, _declared = world
    _peer(root, monkeypatch, record=land_run.UNREADABLE_HALT_RECORD)
    out = _run(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]


def test_a_withheld_path_no_foreign_run_holds_stays_a_blocker(world, monkeypatch) -> None:
    """The index lists it but the landing run's own lineage holds it: not a peer's hunk."""
    root, _orig, _declared = world
    _peer(root, monkeypatch)
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *_a, **_k: {})
    out = _run(root)
    assert out["foreign_red"] == {} and out["unattributed"] == [NODE]
