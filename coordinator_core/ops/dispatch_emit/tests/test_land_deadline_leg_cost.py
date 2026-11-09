"""Per-leg process time and spawn count in the land_deadline ledger, the halt reply and `leg_costs()`."""

import sys

import pytest

from coordinator_core.ops.dispatch_emit import land_deadline
from coordinator_core.telemetry import spawn_counter

pytestmark = pytest.mark.complete

BUDGET = land_deadline.LAND_BUDGET_SECS


class _Clocks:
    def __init__(self):
        self.now = 1000.0
        self.cpu = 50.0


@pytest.fixture
def clocks(monkeypatch):
    c = _Clocks()
    monkeypatch.setattr(land_deadline, "_now", lambda: c.now)
    monkeypatch.setattr(land_deadline, "_cpu", lambda: c.cpu)
    return c


def _spawn(n=1):
    for _ in range(n):
        sys.audit("subprocess.Popen", "git", ["git"], None, None)


def _by_leg(legs):
    return {entry["leg"]: entry for entry in legs}


def test_halt_reply_legs_carry_process_time_and_spawns(clocks):
    with land_deadline.scope():
        clocks.now += 0.5
        clocks.cpu += 0.1
        land_deadline.check("a")
        clocks.now += 1.0
        clocks.cpu += 0.25
        _spawn(3)
        land_deadline.check("b")
        clocks.now += BUDGET + 0.5
        clocks.cpu += 0.04
        _spawn(2)
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            land_deadline.check("c")
    reply = land_deadline.halt_reply(raised.value)
    legs = _by_leg(reply["leg_costs"])
    assert set(legs) == {land_deadline.SCOPE_LEG, "a", "b"}
    assert legs[land_deadline.SCOPE_LEG] == {
        "leg": land_deadline.SCOPE_LEG, "elapsed_secs": 0.5, "process_time_ms": 100.0, "spawns": 0,
    }
    assert legs["a"]["process_time_ms"] == 250.0 and legs["a"]["spawns"] == 3
    assert legs["a"]["elapsed_secs"] == pytest.approx(1.0)
    assert legs["b"]["process_time_ms"] == 40.0 and legs["b"]["spawns"] == 2
    assert reply["leg"] == "c" and reply["refused"] == land_deadline.REFUSED
    assert {"committed", "sha", "error", "queue_wait_secs", "bound_by"} <= set(reply)


def test_leg_costs_names_each_started_leg_up_to_now(clocks):
    with land_deadline.scope():
        land_deadline.check("a")
        clocks.cpu += 0.3
        _spawn(4)
        clocks.now += 2.0
        costs = land_deadline.leg_costs()
    assert [c["leg"] for c in costs] == [land_deadline.SCOPE_LEG, "a"]
    assert costs[1]["process_time_ms"] == 300.0
    assert costs[1]["spawns"] == 4
    assert costs[1]["elapsed_secs"] == pytest.approx(2.0)


def test_leg_costs_is_empty_outside_a_scope():
    assert land_deadline.leg_costs() == []


def test_spawns_outside_a_scope_are_not_counted_inside_one(clocks):
    _spawn(5)
    with land_deadline.scope():
        land_deadline.check("a")
        _spawn(1)
        costs = _by_leg(land_deadline.leg_costs())
    assert costs["a"]["spawns"] == 1
    assert costs[land_deadline.SCOPE_LEG]["spawns"] == 0
    assert spawn_counter.scope_spawns() == 0


def test_spawn_scope_is_a_noop_unbound_and_leaves_the_process_counter_running():
    before = spawn_counter.spawn_count()
    _spawn(2)
    assert spawn_counter.scope_spawns() == 0
    assert spawn_counter.spawn_count() == before + 2


def test_non_spawn_audit_events_are_not_counted(clocks):
    with land_deadline.scope():
        land_deadline.check("a")
        sys.audit("open", "x", None, 0)
        costs = _by_leg(land_deadline.leg_costs())
    assert costs["a"]["spawns"] == 0


def test_reserved_leg_open_still_reads_entry_names(clocks):
    with land_deadline.scope():
        land_deadline.check("land_run.integrity")
        clocks.now += BUDGET + 1.0
        land_deadline.check("peer_hunk_gate.sibling_spine")


def _no_parse(monkeypatch):
    from coordinator_core.authoring_leaks import import_closure

    import_closure._target_bound_names.cache_clear()

    def boom(text):
        raise AssertionError("target parsed")

    monkeypatch.setattr(import_closure, "_parse", boom)
    return import_closure


@pytest.mark.parametrize(
    "text",
    ["def f():\n    pass\n", "async def f():\n    pass\n", "class f:\n    pass\n", "f = 1\n", "f: int\n", "from m import f\n", "import f\n"],
)
def test_import_closure_answers_a_column_zero_binding_without_parsing(monkeypatch, text):
    ic = _no_parse(monkeypatch)
    assert ic._Names(text).binds("f") is True


@pytest.mark.parametrize(
    "text",
    ["from f import g\n", "import a.f\n", "def other():\n    def f():\n        pass\n", "x = 1\nif f == 2:\n    pass\n", "def g():\n    f = 1\n", "# def f\n"],
)
def test_import_closure_still_refuses_a_name_no_module_level_binding_holds(text):
    from coordinator_core.authoring_leaks import import_closure

    import_closure._target_bound_names.cache_clear()
    assert import_closure._Names(text).binds("f") is False


def test_import_closure_reads_an_indented_module_level_binding_through_the_parse():
    from coordinator_core.authoring_leaks import import_closure

    import_closure._target_bound_names.cache_clear()
    assert import_closure._Names("if True:\n    def f():\n        pass\n").binds("f") is True


def test_import_closure_reports_a_star_import_target_as_unknown():
    from coordinator_core.authoring_leaks import import_closure

    import_closure._target_bound_names.cache_clear()
    assert import_closure._Names("from m import *\n").binds("f") is None


def test_mutual_halted_holds_reads_the_holder_claim_view_once_for_all_paths(monkeypatch, tmp_path):
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run, peer_hunk_gate, sibling_claims

    reads = []
    paths = [f"a/p{i}.py" for i in range(7)]
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, run_id: True)
    monkeypatch.setattr(halted_run, "halted_withheld", lambda rec: paths[:6])
    monkeypatch.setattr(halted_hold_release, "held_paths", lambda *a, **k: paths[:5])

    def mirror(root, holder, candidates, **k):
        reads.append(list(candidates))
        return {p: ("own" if p != "a/p1.py" else "other") for p in candidates}

    monkeypatch.setattr(sibling_claims, "sibling_run_claims", mirror)

    got = peer_hunk_gate._mutual_halted_holds(tmp_path, "own", "holder", paths)

    assert reads == [paths[:5]]
    assert got == {"a/p0.py", "a/p2.py", "a/p3.py", "a/p4.py"}


def test_mutual_halted_holds_is_empty_for_a_live_landing_run(monkeypatch, tmp_path):
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate, sibling_claims

    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, run_id: run_id == "holder")
    monkeypatch.setattr(sibling_claims, "sibling_run_claims", lambda *a, **k: pytest.fail("mirror read"))

    assert peer_hunk_gate._mutual_halted_holds(tmp_path, "own", "holder", ["a/p0.py"]) == set()


# Pre-leg budget consumer, profiled with `land_run_leg_budget_profile.py --siblings 1200 --tracked 300
# --chain 50 --attribute` at base: land_run.script_read -> land_run.integrity cost 1062ms process time
# (a 3109ms cProfile span), all of it record-dir scans that grow with the record root --
# `land_run.foreign_halted_withheld` -> `sibling_claims._unlanded_run_ids` (`resumed_run_released` once
# per fresh-token sibling run) and `warp_run_deliverable.live_run_write_claims`. Zero spawns before
# the integrity leg. A run older than the token TTL is never opened, so the state/warp shape (~3000
# record dirs, hundreds in the TTL) is bounded by its in-TTL runs; this pins that shape.
PRE_LEG_BAR_MS = 500.0
RECORD_ROOT_ENTRIES = 1100
IN_TTL_SIBLINGS = 500


def _aged_record_dirs(root, count):
    import json
    from datetime import datetime, timedelta, timezone

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    for i in range(count):
        stamp = (datetime.now(timezone.utc) - timedelta(days=3, minutes=i)).strftime("%Y%m%dT%H%M%S")
        run_id = f"plan-{stamp}-{i:06x}"
        target = root / RECORD_DIR_ROOT / run_id / "run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"run_id": run_id}), encoding="utf-8", newline="\n")


@pytest.mark.spawns_process
def test_a_reviewed_run_over_a_large_record_root_starts_integrity_and_commit_inside_the_budget(tmp_path, monkeypatch):
    """Profile at base (see the block above): the pre-integrity leg is record-dir scans, zero spawns."""
    from contextlib import ExitStack

    from coordinator_core.benchmarks import land_run_leg_budget_profile as lb
    from coordinator_core.benchmarks import land_run_slow_disk_profile as slow

    monkeypatch.setenv("COORDINATOR_SESSION_ID", slow.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with ExitStack() as stack:
        root, base = slow.build_land_fixture(tmp_path, mode="clean", tracked=20, chain=3, stack=stack)
        _aged_record_dirs(root, RECORD_ROOT_ENTRIES)
        whole = lb.profile_land(root, base, attribute=False)[0]

    attribution = whole.attribution
    assert attribution["outcome"] == "committed", attribution["error"]
    assert attribution["land_deadline_halt"] is False
    assert attribution["commit_leg_reached"] is True
    legs = attribution["legs"]
    reached = {leg["leg"] for leg in legs} | {leg["to"] for leg in legs}
    assert {lb.LEG_INTEGRITY, lb.LEG_COMMIT} <= reached
    pre_leg = 0.0
    for leg in legs:
        if leg["leg"] == lb.LEG_INTEGRITY:
            break
        pre_leg += leg["process_time_ms"]
        assert leg["spawns"] == 0, leg
    assert pre_leg < PRE_LEG_BAR_MS, legs


def test_mark_names_a_span_without_refusing_and_check_still_refuses(clocks):
    with land_deadline.scope():
        land_deadline.check("a")
        clocks.now += 4.0
        land_deadline.mark("scan")
        clocks.now += BUDGET + 1.0
        land_deadline.mark("scan.late")
        with pytest.raises(land_deadline.LandDeadlineExceeded) as raised:
            land_deadline.check("b")
    legs = _by_leg(raised.value.legs)
    assert legs["a"]["elapsed_secs"] == pytest.approx(4.0)
    assert legs["scan"]["elapsed_secs"] == pytest.approx(BUDGET + 1.0)
    assert "scan.late" in legs


def test_mark_outside_a_scope_is_a_noop():
    land_deadline.mark("scan")
    assert land_deadline.leg_costs() == []


@pytest.mark.spawns_process
def test_a_reviewed_run_over_in_ttl_sibling_runs_starts_integrity_and_commit_inside_the_budget(tmp_path, monkeypatch):
    """In-TTL sibling runs (token, sizing, spine; no halted marker) cost the pre-integrity leg no unlanded-run listing."""
    from contextlib import ExitStack

    from coordinator_core.benchmarks import land_run_leg_budget_profile as lb
    from coordinator_core.benchmarks import land_run_slow_disk_profile as slow
    from coordinator_core.ops.dispatch_emit import sibling_claims

    monkeypatch.setenv("COORDINATOR_SESSION_ID", slow.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    real = sibling_claims._unlanded_run_ids
    calls = []

    def counted(root, own_run_id, *a, **k):
        calls.append(k.get("covering"))
        return real(root, own_run_id, *a, **k)

    with ExitStack() as stack:
        root, base = slow.build_land_fixture(tmp_path, mode="clean", tracked=20, chain=3, stack=stack)
        lb.add_siblings(root, IN_TTL_SIBLINGS)
        monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", counted)
        whole = lb.profile_land(root, base, attribute=False)[0]

    attribution = whole.attribution
    assert attribution["outcome"] == "committed", attribution["error"]
    assert attribution["land_deadline_halt"] is False
    assert attribution["commit_leg_reached"] is True
    legs = attribution["legs"]
    reached = {leg["leg"] for leg in legs} | {leg["to"] for leg in legs}
    assert {lb.LEG_INTEGRITY, lb.LEG_COMMIT} <= reached
    pre_leg = 0.0
    for leg in legs:
        if leg["leg"] == lb.LEG_INTEGRITY:
            break
        pre_leg += leg["process_time_ms"]
        assert leg["spawns"] == 0, leg
    assert pre_leg < PRE_LEG_BAR_MS, legs
    assert calls, "the foreign-halted listing never ran"


def test_foreign_halted_withheld_listing_skips_token_only_runs_and_keeps_halted_ones(tmp_path, monkeypatch):
    import json

    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)

    from coordinator_core.benchmarks import land_run_leg_budget_profile as lb
    from coordinator_core.ops.dispatch_emit import halted_run, land_run, sibling_claims
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    (tmp_path / ".git").mkdir()
    token_only, halted = lb.add_siblings(tmp_path, 2)
    marker = tmp_path / RECORD_DIR_ROOT / halted / halted_run.HALTED_FILE
    marker.write_text(json.dumps({"status": halted_run.STATUS_HALTED}), encoding="utf-8")

    plain = sibling_claims._unlanded_run_ids(tmp_path, "own", None)
    narrowed = sibling_claims._unlanded_run_ids(tmp_path, "own", None, minted_before=land_run._NO_SENIOR_TOKEN_RUN)

    assert token_only in plain
    assert token_only not in narrowed
    assert halted in plain and halted in narrowed


def _record_root_listings(monkeypatch):
    """Count every `os.scandir`/`os.listdir` of the record root; returns the call list."""
    import os

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    listings = []
    real_scandir, real_listdir = os.scandir, os.listdir

    def watch(path):
        if str(path).replace("\\", "/").rstrip("/").endswith(RECORD_DIR_ROOT):
            listings.append(str(path))

    def scandir(path=".", *a, **k):
        watch(path)
        return real_scandir(path, *a, **k)

    def listdir(path=".", *a, **k):
        watch(path)
        return real_listdir(path, *a, **k)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    return listings


def _halted_sibling_root(tmp_path, monkeypatch):
    import json

    from coordinator_core.benchmarks import land_run_leg_budget_profile as lb
    from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    (tmp_path / ".git").mkdir()
    _token_only, halted = lb.add_siblings(tmp_path, 2)
    marker = tmp_path / RECORD_DIR_ROOT / halted / halted_run.HALTED_FILE
    marker.write_text(json.dumps({"status": halted_run.STATUS_HALTED}), encoding="utf-8")
    halted_withheld_index.rebuild(tmp_path)
    return halted


def test_pre_integrity_scans_list_the_record_root_once_per_release_scope(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_hold_release, land_run, sibling_claims

    _halted_sibling_root(tmp_path, monkeypatch)
    listings = _record_root_listings(monkeypatch)

    with halted_hold_release.release_scope():
        sibling_claims.halted_records(tmp_path, "own", ["a/p.py"])
        land_run._read_markers_strict(tmp_path, "own")
        land_run.foreign_halted_withheld(tmp_path, "own", None, ["a/p.py"])
        land_run._read_markers_strict(tmp_path, "own")

    assert len(listings) == 1, listings


def test_read_markers_strict_still_refuses_a_corrupt_in_ttl_halt_marker(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run, land_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    halted = _halted_sibling_root(tmp_path, monkeypatch)
    (tmp_path / RECORD_DIR_ROOT / halted / halted_run.HALTED_FILE).write_text("{not json", encoding="utf-8")

    with halted_hold_release.release_scope():
        with pytest.raises(ValueError):
            land_run._read_markers_strict(tmp_path, "own")
        held = land_run.foreign_halted_withheld(tmp_path, "own", None, ["a/p.py"])
        assert held == {"a/p.py": land_run.UNREADABLE_HALT_RECORD}
        land_run._read_markers_strict(tmp_path, halted)
