"""A halted run withholds every path its measured-rev records as its own change, declared or not."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_land_run_halt_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _record,
    _write,
    repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OWN = "pkg/review_prep.py"
OWN_REMOVED = "pkg/gone.py"
UNCHANGED = "pkg/same.py"
PEER = "pkg/peer.py"


@pytest.fixture
def measured(repo, monkeypatch):
    """Own changes recorded only in measured-rev.json (no touch record, no chunk declares them)."""
    for rel in (OWN, OWN_REMOVED, UNCHANGED, PEER):
        _write(repo, rel, "v = 0\n")
    _git(repo, "add", "pkg")
    _git(repo, "commit", "-q", "-m", "seed pkg")
    _write(repo, OWN, "v = 1\n")
    (repo / OWN_REMOVED).unlink()
    _write(repo, PEER, "v = 99\n")
    _record(repo)
    _write(
        repo,
        f"{RECORD_DIR_ROOT}/{RUN}/measured-rev.json",
        json.dumps({"paths": [OWN, UNCHANGED, "scratch/x.txt"], "removed": [OWN_REMOVED]}),
    )
    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *_a, **_k: [])
    return repo


def test_measured_own_changes_are_withheld_and_peer_dirt_is_not(measured):
    out = _call(measured, LEDGER, **PASSING)

    assert out["withheld"][OWN] == halted_record_commit.RUN_DIRT_UNCOMMITTED
    assert out["withheld"][OWN_REMOVED] == halted_record_commit.RUN_DIRT_UNCOMMITTED
    assert UNCHANGED not in out["withheld"]
    assert PEER not in out["withheld"]
    assert "scratch/x.txt" not in out["withheld"]


def test_red_without_the_measured_paths(measured, monkeypatch):
    monkeypatch.setattr(land_run_halt, "_measured_own_paths", lambda *_a, **_k: [])

    out = _call(measured, LEDGER, **PASSING)

    assert OWN not in (out.get("withheld") or {})
