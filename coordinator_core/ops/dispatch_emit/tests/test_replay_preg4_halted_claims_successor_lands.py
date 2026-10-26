"""P-REG4 replays: a released halted run's claim never holds its successor (25583b); the landing
session is never its own holder (bd6507); out-of-ask holds are bounded to the run's start-base diff (08132c)."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import peer_hunk_gate, shared_path_landing as spl
from coordinator_core.ops.dispatch_emit.tests.test_peer_hunk_gate import (  # noqa: F401
    FINDING, OWN, PATH, SELF, _record, _release_fixture, _sidecar, repo,
)

UNLANDED = [("C1", PATH, "peer-held")]


def test_a_released_halted_run_never_holds_its_successor(repo):
    halted = _release_fixture(repo, "superseded")
    held = {PATH: [halted]}
    assert spl.sibling_held_product(UNLANDED, held, root=repo, own_run_id=OWN) == []


def test_a_halted_run_still_holding_refuses_the_successor(repo):
    halted = _release_fixture(repo, "open")
    held = {PATH: [halted]}
    assert spl.sibling_held_product(UNLANDED, held, root=repo, own_run_id=OWN) == [("C1", PATH, [halted])]


def test_a_successor_on_the_same_sizing_releases_the_halted_run(repo):
    halted = _release_fixture(repo, "open")
    rdir = repo / "state" / "warp" / OWN
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    assert spl.sibling_held_product(UNLANDED, {PATH: [halted]}, root=repo, own_run_id=OWN) == []


def test_the_landing_session_is_never_its_own_holder(repo):
    assert spl.sibling_held_product(UNLANDED, {PATH: [SELF]}, own_session=SELF) == []
    assert spl.sibling_held_product(UNLANDED, {PATH: [SELF, "other-sid"]}, own_session=SELF) == [
        ("C1", PATH, ["other-sid"])
    ]
    assert spl.sibling_held_product(UNLANDED, {PATH: [SELF]}) == [("C1", PATH, [SELF])]


def test_a_session_holder_is_not_a_released_run(repo):
    assert spl.released_holder(repo, SELF, OWN) is False


def test_gate_releases_the_superseded_predecessor_and_still_refuses_a_different_plan_peer(repo):
    halted = _release_fixture(repo, "open", plan="docs/plans/other.md")
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], own_plan="docs/plans/p.md") == [
        (PATH, halted)
    ]
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], own_plan="docs/plans/other.md") == []
    _release_fixture(repo, "superseded", plan="docs/plans/other.md")
    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, [PATH], [PATH], own_plan="docs/plans/p.md") == []


def test_a_land_only_ask_is_never_held_by_its_own_halted_run(repo):
    halted = _release_fixture(repo, "open")
    held = {PATH: [halted]}
    assert spl.sibling_held_product(UNLANDED, held, root=repo, own_run_id=halted) == []
    assert spl.sibling_held_product(UNLANDED, held, root=repo, own_run_id=OWN) == [("C1", PATH, [halted])]


BASE = "b" * 40


def test_review_red_baseline_is_head_at_review_start_and_credits_an_owned_red():
    from coordinator_core.ops.review_mint.execute_review import _base_measure_clause

    clause = _base_measure_clause(BASE, "state/warp/run1/owned_red.jsonl")
    assert "--sha HEAD" in clause and f"--sha {BASE}" in clause
    assert "state/warp/run1/owned_red.jsonl" in clause and "owner_run_id" in clause
    assert "owned_red" not in _base_measure_clause(BASE)
