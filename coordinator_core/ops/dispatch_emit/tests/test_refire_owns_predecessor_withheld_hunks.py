"""A refire's peer split attributes hunks its same-sizing predecessor withheld to the refire, never `unknown`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index as hwi
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import run_artifact_ownership as rao
from coordinator_core.ops.dispatch_emit.tests.test_hunk_provenance import (  # noqa: F401 -- `world` is the fixture
    ME,
    PATH,
    _text,
    world,
)

PRED = "ask-20261024T000000-cccccc"
SIZING = "state/sizings/2026-11/a.yaml"


@pytest.fixture
def refire(world, monkeypatch):
    """Example-Game-Repo shape: the predecessor withheld its hunk at line 2 (carried into the refire's baseline), C1 wrote line 8."""
    from coordinator_core.ops.dispatch_emit import co_declared_settle

    monkeypatch.setattr(co_declared_settle, "settled_blob", lambda *a, **k: "d" * 40)
    world.head(_text({}))
    world.baseline(_text({2: "PRED"}))
    world.tree(_text({2: "PRED", 8: "C1"}))
    return world


def _withhold(world, run: str, text: bytes, *, sizing: str = SIZING) -> None:
    hwi.add_run(world.root, run, [PATH], sizing_rel=sizing, prints={PATH: world.store(text)}, at_ns=1)


def _predecessors(monkeypatch, *runs: str) -> None:
    monkeypatch.setattr(rao, "own_artifact_paths", lambda root, run: rao.OwnArtifacts(predecessors=tuple(runs)))


def test_unattributed_without_a_predecessor_withheld_print(refire, monkeypatch):
    _predecessors(monkeypatch, PRED)
    split, _ = hp.peer_split(refire.root, ME, [PATH])
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER and split[PATH]["lines"] == "2,1"
    assert [f.reason for f in hp.unproduced_hunks(refire.root, ME, [PATH], own_plan=None)] == [hp.FOREIGN_HUNK_UNPRODUCED]


def test_same_sizing_predecessor_withheld_hunk_is_the_refires_own(refire, monkeypatch):
    _predecessors(monkeypatch, PRED)
    _withhold(refire, PRED, _text({2: "PRED", 5: "PRED2"}))
    assert hp.peer_split(refire.root, ME, [PATH]) == ({}, {})
    assert hp.unproduced_hunks(refire.root, ME, [PATH], own_plan=None) == []


def test_a_hunk_the_withheld_print_does_not_carry_stays_a_peer_finding(refire, monkeypatch):
    _predecessors(monkeypatch, PRED)
    _withhold(refire, PRED, _text({2: "OTHER"}))
    split, _ = hp.peer_split(refire.root, ME, [PATH])
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER and split[PATH]["lines"] == "2,1"


def test_a_run_that_is_not_a_same_sizing_predecessor_is_not_credited(refire, monkeypatch):
    _predecessors(monkeypatch)
    _withhold(refire, PRED, _text({2: "PRED"}), sizing="state/sizings/2026-11/other.yaml")
    split, _ = hp.peer_split(refire.root, ME, [PATH])
    assert split[PATH]["owner"] == hp.UNKNOWN_OWNER and split[PATH]["lines"] == "2,1"


def test_withheld_prints_are_read_once_per_run(refire, monkeypatch):
    _predecessors(monkeypatch, PRED)
    _withhold(refire, PRED, _text({2: "PRED"}))
    reads: list = []
    real = hwi.run_entry
    monkeypatch.setattr(hwi, "run_entry", lambda root, run: reads.append(run) or real(root, run))
    with hp.invariants_scope(refire.root, ME):
        hp.peer_split(refire.root, ME, [PATH])
        hp.unproduced_hunks(refire.root, ME, [PATH], own_plan=None)
    assert reads == [PRED]
