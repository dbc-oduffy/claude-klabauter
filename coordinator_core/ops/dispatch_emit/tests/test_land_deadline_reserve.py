"""The integrity and commit legs draw on a reserved tail of the land_run budget."""

from __future__ import annotations

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import land_deadline

BUDGET = land_deadline.LAND_BUDGET_SECS
RESERVE = land_deadline.RESERVE_SECS


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(land_deadline, "_now", fake)
    return fake


@pytest.mark.parametrize(
    "leg",
    [
        "land_run.integrity",
        "land_run.commit",
        "land_run_halt._land.integrity",
        "land_run_halt._land.commit",
        "land_run._integrity.since_base",
    ],
)
def test_slow_earlier_leg_still_lets_reserved_leg_start(clock, leg):
    with land_deadline.scope():
        land_deadline.check("land_run.script_read")
        clock.now += BUDGET + 0.5
        land_deadline.check(leg)


@pytest.mark.parametrize("leg", ["land_run.script_read", "land_run_halt._land.claims", "land_run.other"])
def test_optional_leg_halts_inside_the_reserve(clock, leg):
    with land_deadline.scope():
        clock.now += BUDGET + 0.5
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check(leg)
    assert exc.value.leg == leg


def test_leg_after_an_open_integrity_leg_draws_on_the_reserve(clock):
    with land_deadline.scope():
        land_deadline.check("land_run.integrity")
        clock.now += BUDGET + 1.0
        land_deadline.check("peer_hunk_gate.sibling_spine")


def test_leg_without_an_open_integrity_leg_halts_inside_the_reserve(clock):
    with land_deadline.scope():
        clock.now += BUDGET + 1.0
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            land_deadline.check("peer_hunk_gate.sibling_spine")


def test_nested_reserved_leg_is_still_bounded_by_the_hard_deadline(clock):
    with land_deadline.scope():
        land_deadline.check("land_run.integrity")
        clock.now += BUDGET + RESERVE + 0.1
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("peer_hunk_gate.sibling_spine")
    assert exc.value.bound_by == land_deadline.BOUND_BUDGET


def test_integrity_open_in_one_scope_does_not_reserve_the_next(clock):
    with land_deadline.scope():
        land_deadline.check("land_run.integrity")
    with land_deadline.scope():
        clock.now += BUDGET + 1.0
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            land_deadline.check("peer_hunk_gate.sibling_spine")


def test_reserved_leg_halts_past_the_full_budget(clock):
    with land_deadline.scope():
        clock.now += BUDGET + RESERVE + 0.1
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            land_deadline.check("land_run.integrity")
        assert land_deadline.expired()


def test_outer_dispatcher_clamp_binds_reserved_legs(clock, monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 2.0 + dispatch_abandon.REPLY_MARGIN_SECS)
    with land_deadline.scope():
        clock.now += 2.5
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            land_deadline.check("land_run.commit")


def test_server_window_clamp_binds_reserved_legs(clock):
    land_deadline.set_queue_wait(5.0, window_secs=5.0 + 2.0 + dispatch_abandon.REPLY_MARGIN_SECS)
    try:
        with land_deadline.scope():
            clock.now += 2.5
            with pytest.raises(land_deadline.LandDeadlineExceeded):
                land_deadline.check("land_run.integrity")
    finally:
        land_deadline.set_queue_wait(0.0, None)


def test_excepted_retest_time_does_not_spend_the_leg_budget(clock):
    with land_deadline.scope():
        land_deadline.check("land_run.script_read")
        with land_deadline.excepted():
            clock.now += BUDGET + RESERVE + 5.0
        clock.now += 1.0
        land_deadline.check("peer_hunk_gate.co_declared_peer_holds")


def test_excepted_never_moves_a_deadline_past_the_dispatcher_clamp(clock, monkeypatch):
    monkeypatch.setattr(dispatch_abandon, "remaining", lambda: 20.0 + dispatch_abandon.REPLY_MARGIN_SECS)
    with land_deadline.scope():
        with land_deadline.excepted():
            clock.now += 15.0
        clock.now += 5.5
        with pytest.raises(land_deadline.LandDeadlineExceeded):
            land_deadline.check("land_run.commit")


def test_a_first_leg_halt_attributes_the_time_before_it_to_scope_entry(clock):
    with land_deadline.scope():
        clock.now += BUDGET + 1.0
        with pytest.raises(land_deadline.LandDeadlineExceeded) as exc:
            land_deadline.check("land_run.script_read")
    assert exc.value.legs == [{"leg": land_deadline.SCOPE_LEG, "elapsed_secs": BUDGET + 1.0}]


def test_no_deadline_bound_is_a_noop():
    land_deadline.check("land_run.script_read")
    land_deadline.check("land_run.integrity")


def _source_legs(path):
    import re
    from pathlib import Path

    return re.findall(r'land_deadline\.check\("([^"]+)"\)', Path(path).read_text(encoding="utf-8"))


def test_landing_sources_pass_reserved_leg_names_to_the_deadline():
    from coordinator_core.ops.dispatch_emit import land_run, land_run_halt

    run_legs = _source_legs(land_run.__file__)
    halt_legs = _source_legs(land_run_halt.__file__)
    reserved = lambda legs, tail: [leg for leg in legs if land_deadline._is_reserved(leg) and leg.endswith(tail)]
    assert reserved(run_legs, "integrity") and reserved(run_legs, "commit")
    assert reserved(halt_legs, "integrity") and reserved(halt_legs, "commit")
    assert [leg for leg in run_legs + halt_legs if not land_deadline._is_reserved(leg)]


def test_superseded_prefilter_yields_the_same_runs_as_the_unfiltered_scan(tmp_path, monkeypatch):
    import json

    from coordinator_core.ops.dispatch_emit import land_run, run_artifacts, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    sizing = "state/sizings/2026-10-16-design.yaml"
    mine = "ask-20261016T120000-aaaaaa"
    dirs = {
        "ask-20261016T100000-bbbbbb": {"sizing_rel": sizing},
        "ask-20261016T110000-cccccc": {"sizing_rel": "state/sizings/other.yaml"},
        "ask-20261016T130000-dddddd": {"sizing_rel": sizing},
        mine: {"sizing_rel": sizing},
    }
    for name, body in dirs.items():
        d = tmp_path / RECORD_DIR_ROOT / name
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(run_token, "held_records", lambda _root: [])
    seen = []
    real = run_artifacts.superseded_runs

    def spy(root, run_id, sizing_rel, changed, live_run_ids=()):
        changed = list(changed)
        seen.append(changed)
        return real(root, run_id, sizing_rel, changed, live_run_ids=live_run_ids)

    monkeypatch.setattr(run_artifacts, "superseded_runs", spy)
    land_run._superseded(tmp_path, mine, sizing, (), run_token)
    unfiltered = real(
        tmp_path, mine, sizing, [f"{RECORD_DIR_ROOT}/{n}/run.json" for n in dirs], live_run_ids=()
    )
    assert unfiltered == ("ask-20261016T100000-bbbbbb",)
    assert real(tmp_path, mine, sizing, seen[0], live_run_ids=()) == unfiltered


def _ledger_gate_probe(tmp_path, monkeypatch, *, dirt_without_credit):
    import json

    from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
    from coordinator_core.ops.dispatch_emit import write_baseline

    prior_calls = []
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *a, **k: object())
    monkeypatch.setattr(requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": []})
    monkeypatch.setattr(land_run, "held_revision_refusal", lambda *a, **k: None)
    monkeypatch.setattr(land_run, "_remnant_breach", lambda *a, **k: None)
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *a, **k: [])
    monkeypatch.setattr(own_checkpoint_paths, "checkpoint_landed_paths", lambda *a, **k: frozenset())
    monkeypatch.setattr(write_baseline, "unwritten", lambda root, baseline, writes: set(writes))
    monkeypatch.setattr(write_baseline, "predecessor_withheld", lambda *a, **k: frozenset())
    monkeypatch.setattr(write_baseline, "prior_pass_written", lambda *a, **k: prior_calls.append(1) or frozenset({"a.py"}))

    def dirt(root, baseline, writes, credit):
        return frozenset() if ("a.py" in credit or not dirt_without_credit) else frozenset({"a.py"})

    monkeypatch.setattr(write_baseline, "pre_existing_dirt", dirt)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"write_baseline": {"x": 1}, "rows": [{"register_rows": ["R1"], "writes": ["a.py"]}]}),
        encoding="utf-8",
    )
    verdict = {"items": [{"id": "R1", "status": "met"}]}
    refusal, _ledger, _open = land_run._ledger_gate(
        tmp_path, tmp_path, verdict, manifest, (), {}, None, {"prep": {}}, None, None
    )
    return refusal, prior_calls


def test_ledger_gate_skips_prior_credit_read_when_no_row_shows_dirt(tmp_path, monkeypatch):
    refusal, prior_calls = _ledger_gate_probe(tmp_path, monkeypatch, dirt_without_credit=False)
    assert refusal is None
    assert prior_calls == []


def test_ledger_gate_credits_prior_passes_before_flagging_dirt(tmp_path, monkeypatch):
    refusal, prior_calls = _ledger_gate_probe(tmp_path, monkeypatch, dirt_without_credit=True)
    assert refusal is None
    assert prior_calls == [1]
