"""Two halted runs withholding one path against each other: the path lands whole; a one-sided hold names the route."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_peer_hunk_gate as gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = gate.PATH


def _halt_own(root, run_id: str) -> None:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (rdir / "ask.md").write_text("ask\n", encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {PATH: "peer-held"}}}), encoding="utf-8"
    )


def test_two_halted_runs_withholding_the_same_path_land_it_whole(repo):
    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})
    own = gate._aged(30, "dddddd")
    _halt_own(repo, own)
    prod._author(repo, PATH, "x = 1\n")
    prod._declare(repo, PATH)

    out = prod._land(repo, run_id=own)

    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)


def test_a_halted_holder_withholding_the_path_against_a_run_it_does_not_hold_refuses_naming_the_route(repo):
    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})

    out, head = gate._land_dirty(repo)

    assert out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    text = str(out)
    assert f"Mutual hold: run {holder} withholds {PATH}" in text, out
    assert f"dispatch.record_halted_run_landed {{run_id: {holder}" in text, out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head


def test_a_halted_holder_not_withholding_the_path_names_no_mutual_route(repo):
    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md")

    out, _head = gate._land_dirty(repo)

    assert out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    assert "Mutual hold" not in str(out), out
