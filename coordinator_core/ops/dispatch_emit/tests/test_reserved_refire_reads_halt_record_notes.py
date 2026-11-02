"""A refire minted through `reserved_run_id` reads the revise notes of the halted run the sizing's halt record names."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import parent_record_rel
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    WARP,
    _halt,
    composed,
    repo,
)

PARENT = "ask-parent-20261016T120000-cccccc"
SLICE = "ask-20261016T120001-dddddd"
BASE = "a" * 40
NOTE = "revise: admit the named carve-in"


def _slice_sizing(repo):
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["em_analysis"] = {"ask_parent": f"{PARENT} {SLICE}"}
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (repo / parent_record_rel(PARENT)).parent.mkdir(parents=True)
    (repo / parent_record_rel(PARENT)).write_text("{}\n", encoding="utf-8")


def _note(repo, run_id):
    notes = rl.run_dir(repo, run_id) / rl.NOTES_FILE
    notes.write_text(json.dumps({"note": NOTE, "at": "2026-10-26T10:00:00Z"}) + "\n", encoding="utf-8")


def _fire(repo):
    return op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": SLICE}, repo_root=repo)


def test_a_reserved_refire_carries_the_named_halted_runs_notes(repo, composed):  # noqa: F811
    _slice_sizing(repo)
    _halt(repo, WARP, BASE)
    _note(repo, WARP)
    reply = _fire(repo)
    assert reply["run_id"] == SLICE
    assert rl.read_run(rl.run_dir(repo, SLICE))[rl.RESUMED_FROM_FIELD] == WARP
    assert [e["note"] for e in rl.carried_notes(repo, SLICE)] == [NOTE]


def test_a_reserved_first_fire_resumes_nothing(repo, composed):  # noqa: F811
    _slice_sizing(repo)
    reply = _fire(repo)
    assert reply["run_id"] == SLICE
    assert rl.RESUMED_FROM_FIELD not in rl.read_run(rl.run_dir(repo, SLICE))
    assert rl.carried_notes(repo, SLICE) == []
