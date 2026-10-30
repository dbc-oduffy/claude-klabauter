"""Leg-budget pins for `dispatch.land_run`: sibling spines are read once per op, the
script_read-to-integrity span spawns nothing, and an indexed sibling's claim costs no per-sibling parse.
Counts, never wall clock (peer load moves wall)."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof
from coordinator_core.ops.dispatch_emit import halted_withheld_index, peer_hunk_gate, run_token

SIBLINGS = 6


@pytest.fixture(scope="module")
def rows(tmp_path_factory) -> dict:
    out = prof.run(tmp_path_factory.mktemp("lrlb"), SIBLINGS, prof.DEFAULT_TRACKED, prof.DEFAULT_CHAIN, attribute=False)
    return {r.axis: r for r in out}


def test_clean_landing_commits(rows) -> None:
    assert rows[prof.WHOLE_OP].attribution["outcome"] == "committed"


def test_each_sibling_spine_parsed_once_across_the_op(rows) -> None:
    whole = rows[prof.WHOLE_OP]
    assert whole.attribution["sizing_parses"] == SIBLINGS
    assert whole.attribution["plan_parses"] == SIBLINGS


def test_holds_and_spine_legs_do_not_reparse(rows) -> None:
    assert rows[prof.AXIS_SPINE].spine_parses == 2 * SIBLINGS
    assert rows[prof.AXIS_HOLDS].spine_parses == rows[prof.AXIS_SPINE].spine_parses


def test_read_to_integrity_span_is_measured_and_spawns_nothing(rows) -> None:
    span = rows[prof.AXIS_READ_TO_INTEGRITY]
    assert span.attribution["measured"] is True
    assert span.spawns == 0


PATH = "pkg/shared.py"
OWN = "plan-20200101T000000-aaaaaa"


def _live_sibling(root, run_id: str, declared: str, *, indexed: bool) -> None:
    """An unlanded sibling with a fresh token and no manifest or spine on disk."""
    (root / ".git").mkdir(exist_ok=True)
    (root / "state" / "warp" / run_id).mkdir(parents=True, exist_ok=True)
    run_token.mint(root, run_id, "run-sib.mjs", session_id="00000000-0000-4000-8000-000000000001")
    if indexed:
        halted_withheld_index.record_declared(root, run_id, [declared])


def _count_parses(monkeypatch) -> dict:
    from coordinator_core.ops import warp_run_deliverable

    calls = {"manifest": 0, "spine": 0}
    real_manifest, real_spine = warp_run_deliverable._manifest_declared, peer_hunk_gate._sibling_spines

    def manifest(*a, **k):
        calls["manifest"] += 1
        return real_manifest(*a, **k)

    def spine(root, run_ids):
        calls["spine"] += len(list(run_ids))
        return real_spine(root, run_ids)

    monkeypatch.setattr(warp_run_deliverable, "_manifest_declared", manifest)
    monkeypatch.setattr(peer_hunk_gate, "_sibling_spines", spine)
    return calls


def test_indexed_sibling_is_named_from_the_index_with_no_per_sibling_parse(tmp_path, monkeypatch) -> None:
    halted_withheld_index.rebuild(tmp_path)
    siblings = [prof.sibling_run_id(i) for i in range(SIBLINGS)]
    for run_id in siblings:
        _live_sibling(tmp_path, run_id, PATH, indexed=True)
    calls = _count_parses(monkeypatch)

    holds = peer_hunk_gate.co_declared_peer_holds(tmp_path, OWN, [PATH], [PATH])

    assert sorted(s for _p, s in holds) == sorted(siblings)
    assert calls == {"manifest": 0, "spine": 0}


def test_indexed_sibling_declaring_another_path_holds_nothing(tmp_path) -> None:
    halted_withheld_index.rebuild(tmp_path)
    _live_sibling(tmp_path, prof.sibling_run_id(0), "pkg/other.py", indexed=True)

    assert peer_hunk_gate.co_declared_peer_holds(tmp_path, OWN, [PATH], [PATH]) == []


def test_unindexed_sibling_still_reads_its_own_records(tmp_path, monkeypatch) -> None:
    halted_withheld_index.rebuild(tmp_path)
    run_id = prof.sibling_run_id(0)
    _live_sibling(tmp_path, run_id, PATH, indexed=False)
    calls = _count_parses(monkeypatch)

    peer_hunk_gate.co_declared_peer_holds(tmp_path, OWN, [PATH], [PATH])

    assert calls["manifest"] >= 1
    assert halted_withheld_index.entries(tmp_path) == {}


SIZING = "state/sizings/2026-10-29-some-sizing-abcdef.yaml"
MINE = "warp-x-20261029T111243-3cb988"


def _record(root, name: str, sizing: str) -> None:
    home = root / "state" / "warp" / name
    home.mkdir(parents=True)
    (home / "run.json").write_text('{"sizing_rel": "%s"}' % sizing, encoding="utf-8")


def test_superseded_scan_hands_only_earlier_same_sizing_records_to_the_parser(tmp_path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import land_run, run_artifacts

    _record(tmp_path, "warp-a-20261029T100000-aaaaaa", SIZING)
    _record(tmp_path, "warp-b-20261029T100001-bbbbbb", "state/sizings/other.yaml")
    _record(tmp_path, "warp-c-20261029T120000-cccccc", SIZING)
    seen: list = []
    monkeypatch.setattr(run_artifacts, "superseded_runs", lambda root, rid, sizing, changed, live_run_ids=(): seen.append(list(changed)) or ())

    class Tokens:
        @staticmethod
        def held_records(_root):
            return []

    assert land_run._superseded(tmp_path, MINE, SIZING, None, Tokens) == ((), set())
    assert seen == [["state/warp/warp-a-20261029T100000-aaaaaa/run.json"]]


def _handed_to_parser(tmp_path, monkeypatch, sizing: str) -> list:
    from coordinator_core.ops.dispatch_emit import land_run, run_artifacts

    seen: list = []
    monkeypatch.setattr(run_artifacts, "superseded_runs", lambda root, rid, s, changed, live_run_ids=(): seen.append(list(changed)) or ())

    class Tokens:
        @staticmethod
        def held_records(_root):
            return []

    land_run._superseded(tmp_path, MINE, sizing, None, Tokens)
    return sorted(p.split("/")[2] for p in seen[0]) if seen else []  # seen[0]: run.json paths handed on


def test_superseded_scan_skips_records_older_than_the_sizing_but_keeps_zone_slack(tmp_path, monkeypatch) -> None:
    _record(tmp_path, "warp-old-20261020T100000-111111", SIZING)
    _record(tmp_path, "warp-eve-20261028T230000-222222", SIZING)
    _record(tmp_path, "warp-day-20261029T010000-333333", SIZING)

    assert _handed_to_parser(tmp_path, monkeypatch, SIZING) == [
        "warp-day-20261029T010000-333333", "warp-eve-20261028T230000-222222",
    ]


def test_superseded_scan_reads_every_earlier_record_for_an_undated_sizing(tmp_path, monkeypatch) -> None:
    undated = "state/sizings/some-sizing.yaml"
    _record(tmp_path, "warp-old-20261020T100000-111111", undated)

    assert _handed_to_parser(tmp_path, monkeypatch, undated) == ["warp-old-20261020T100000-111111"]


def test_sizing_stamp_floor_refuses_an_impossible_date(tmp_path) -> None:
    from coordinator_core.ops.dispatch_emit import land_run

    assert land_run._sizing_stamp_floor("2026-13-45-x.yaml") == ""
    assert land_run._sizing_stamp_floor("2026-10-29-x.yaml") == "20261028T000000-000000"
