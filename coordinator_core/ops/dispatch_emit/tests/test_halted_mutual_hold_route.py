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


def test_two_halted_runs_withholding_the_same_path_commit_the_record_and_leave_the_path_dirty(repo):
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
    assert PATH not in prod._tree(repo)


def test_a_halted_holder_withholding_the_path_lands_naming_it_in_provenance(repo):
    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})

    out, head = gate._land_dirty(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert holder in str(gate._prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out
    assert "Mutual hold" not in str(out), out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() != head


def test_a_live_landing_run_is_held_by_a_halted_holder_even_when_the_mirror_view_names_it_first(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import foreign_hunks, sibling_claims

    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})
    own = gate._aged(30, "dddddd")
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: {PATH: own if a[1] == holder else holder})

    assert peer_hunk_gate._mutual_halted_hold(repo, own, holder, PATH) is False
    holds = peer_hunk_gate.co_declared_peer_holds(repo, own, [PATH], [PATH])
    assert [h for h in holds if h == (PATH, holder)], holds

    monkeypatch.setattr(
        foreign_hunks, "foreign_hunks", lambda root, pairs, **k: [(PATH, holder, ["@@ -5,2 +5,3 @@"], True)]
    )
    text = peer_hunk_gate.refusal_message(holds, own, repo)
    assert f"Foreign hunks in run {holder}'s kept blob: {PATH} @@ -5,2 +5,3 @@" in text, text


def test_two_halted_runs_are_mutual_only_when_both_withhold_the_path(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})
    own = gate._aged(30, "dddddd")
    _halt_own(repo, own)
    prod._author(repo, PATH, "x = 1" + chr(10))
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: {PATH: own})

    assert peer_hunk_gate._mutual_halted_hold(repo, own, holder, PATH) is True

    (repo / "state" / "warp" / own / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {}}}), encoding="utf-8"
    )
    assert peer_hunk_gate._mutual_halted_hold(repo, own, holder, PATH) is False


def test_refusal_message_tails_the_uncapped_hunk_count(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import foreign_hunks

    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})
    own = gate._aged(30, "dddddd")
    headers = foreign_hunks.Headers(["@@ -1,1 +1,1 @@"])
    headers.total = 4
    monkeypatch.setattr(foreign_hunks, "foreign_hunks", lambda root, pairs, **k: [(PATH, holder, headers, False)])

    text = peer_hunk_gate.refusal_message([(PATH, holder)], own, repo)

    assert f"Uncommitted hunks on {PATH} (held by run {holder}; may include your own): @@ -1,1 +1,1 @@ (+3 more)." in text, text


def test_a_halted_holder_not_withholding_the_path_lands_and_names_no_mutual_route(repo):
    gate._sizing(repo, gate.SIZING)
    holder = gate._aged(60, "aaaaaa")
    gate._holder(repo, holder, "halted", plan="docs/plans/other.md")

    out, _head = gate._land_dirty(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert holder in str(gate._prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out
    assert "Mutual hold" not in str(out), out
