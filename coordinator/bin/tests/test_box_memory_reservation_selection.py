"""Opt-in own-grant credit never subtracts a peer's reservation or copied authority."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from coordinator_core.group_em import box_registry, nomination, slot_grant
from coordinator_core.session import core

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("selected_box_memory", BIN / "box-memory-snapshot.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
host = mod._load("box_memory_host")
SID, HOLDER = "actual-session", "live-holder"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "real-settings"
    directory = home / "state/group-em"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(core, "attributable_session_id", lambda *a: SID)
    monkeypatch.setattr(nomination, "box_holder", lambda *a, **kw: {"session_id": HOLDER, "live": True})
    monkeypatch.setattr(slot_grant, "session_live", lambda sid: sid in (SID, "peer"))
    monkeypatch.setattr(mod, "_load", lambda name: host)
    return directory


def grant(directory, sid=SID, run="mine", gib=6, issuer=HOLDER, expires=None):
    now = time.time()
    record = {"schema_version": 1, "kind": "group-em-slot-grant", "issuer_session_id": issuer,
        "grantee_session_id": sid, "run": run, "declared_peak_mb": gib * 1024,
        "issued_at": slot_grant._iso(now - 10), "expires_at": slot_grant._iso(expires or now + 600)}
    path = directory / "slot-grants" / issuer / f"{sid}.{run}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return record


def run(directory, *args):
    return mod.main(["--admission-file", str(directory / "box/admission.jsonl"), "--json", *args],
        platform="win32", status_fn=lambda: host.MemoryStatus(10 * host.GIB, 32 * host.GIB, 1 * host.GIB, 64 * host.GIB),
        nonpaged_fn=lambda: None, cache_host_fn=lambda: None, proc_table_fn=lambda: [], cmdline_fn=lambda pid: "",
        rss_fn=lambda pid: 0, alive_fn=lambda pid: True, create_time_fn=lambda pid: 1000,
        image_fn=lambda pid: "python.exe")


def test_own_grant_credit_removes_self_block_but_keeps_peers(env, capsys):
    grant(env)
    grant(env, sid="peer", gib=2)
    assert run(env, "--admit-peak", "6") == 4
    assert run(env, "--admit-peak", "6", "--run", "mine") == 0
    snap = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert snap["admit"]["reserved_gib"] == 2
    assert snap["reservation_selection"]["credited_gib"] == 6


def test_other_run_and_old_issuer_are_not_credited(env, capsys):
    grant(env)
    grant(env, run="other", gib=1)
    grant(env, issuer="stale-holder", gib=2)
    assert run(env, "--admit-peak", "6", "--run", "mine") == 4
    snap = json.loads(capsys.readouterr().out)
    assert snap["admit"]["reserved_gib"] == 3


@pytest.mark.parametrize("problem", ["unknown", "expired", "oversized", "other-session", "box-mismatch"])
def test_invalid_grant_selection_fails_closed(env, problem, capsys):
    record = grant(env)
    args = ["--admit-peak", "6", "--run", "mine"]
    path = env / "slot-grants" / HOLDER / f"{SID}.mine.json"
    if problem == "unknown":
        path.unlink()
    elif problem == "expired":
        record["expires_at"] = slot_grant._iso(time.time() - 1)
        path.write_text(json.dumps(record))
    elif problem == "oversized":
        args[1] = "7"
    elif problem == "other-session":
        args += ["--session-id", "peer"]
    else:
        record["box_id"] = "different-box"
        path.write_text(json.dumps(record))
    assert run(env, *args) == 4
    assert "refused" in capsys.readouterr().err


def test_copied_holder_tree_cannot_supply_grant_authority(env, tmp_path, capsys):
    copied = tmp_path / "copied-settings/state/group-em"
    grant(copied)
    assert run(copied, "--admit-peak", "6", "--run", "mine") == 4
    assert "live matching" in capsys.readouterr().err


def test_concurrent_selected_check_and_register_still_serialize(env):
    grant(env)
    admission = env / "box/admission.jsonl"
    status = host.MemoryStatus(10 * host.GIB, 32 * host.GIB, host.GIB, 64 * host.GIB)
    def worker(pid):
        verdict, reserved, selected = host.admit_selected_reservation(
            admission, 6, status, lambda: slot_grant.own_admission_selector(SID, "mine", 6144),
            key=f"job-{pid}", pid=pid, rss_fn=lambda p: 0, alive_fn=lambda p: True,
            create_time_fn=lambda p: 1000, image_fn=lambda p: "python.exe")
        return verdict.admit
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [101, 102])) == [False, True]


def test_live_start_is_not_erased_by_own_grant_credit(env, capsys):
    grant(env)
    admission = env / "box/admission.jsonl"
    admission.parent.mkdir(parents=True)
    admission.write_text(json.dumps({"phase": "START", "session_id": SID, "run": "mine",
        "pids": [111], "create_time": 1000, "declared_peak_mb": 6144}) + "\n")
    assert run(env, "--admit-peak", "6", "--run", "mine") == 4
    snap = json.loads(capsys.readouterr().out)
    assert snap["admit"]["reserved_gib"] == 6
    assert snap["reservation_selection"]["credited_gib"] == 0


def test_explicit_run_with_unknown_recorded_peak_is_not_a_success(env):
    with pytest.raises(SystemExit) as refused:
        run(env, "--run", "mine", "--command-key", "unrecorded")
    assert refused.value.code == 2


CHILD = "00000000-0000-4000-8000-000000000010"
PARENT = "00000000-0000-4000-8000-000000000011"


@pytest.fixture
def pool(env, monkeypatch):
    from coordinator_core.session import external_registry
    monkeypatch.setattr(core, "attributable_session_id", lambda *a: CHILD)
    monkeypatch.setattr(slot_grant, "session_live", lambda sid: sid in (CHILD, PARENT))
    bindings = {CHILD: {"session_id": CHILD, "parent_session_id": PARENT}, PARENT: {"session_id": PARENT}}
    monkeypatch.setattr(external_registry, "lookup", lambda sid: bindings.get(sid))
    grant(env, sid=PARENT, run="parent-pool", gib=8)
    admission = env / "box/admission.jsonl"
    admission.parent.mkdir(parents=True)
    parent = {"phase": "START", "session_id": PARENT, "run": "parent-pool", "pids": [500],
              "create_time": 1000, "declared_peak_mb": 8192}
    admission.write_text(json.dumps(parent) + "\n")
    return env, bindings, admission, parent


def child_run(directory, name, pid, peak="4"):
    return run(directory, "--run", name, "--parent-pool-run", "parent-pool", "--admit-peak", peak,
               "--command-key", "child", "--register-pid", str(pid))


def test_selected_parent_pool_allocates_within_registered_budget_and_coalesces(pool, capsys):
    directory, _, admission, _ = pool
    assert child_run(directory, "one", 101) == 0
    assert child_run(directory, "two", 102) == 0
    snap = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert snap["reservation_selection"]["allocated_peak_mb"] == 8192
    assert snap["reservation_selection"]["remaining_peak_mb"] == 0
    assert child_run(directory, "three", 103) == 4
    assert "exceed" in capsys.readouterr().err
    assert host.reserved_bytes(admission, rss_fn=lambda p: 0, alive_fn=lambda p: True,
        create_time_fn=lambda p: 1000, image_fn=lambda p: "python.exe") == 8 * host.GIB
    child = [json.loads(line) for line in admission.read_text().splitlines() if '"run": "one"' in line][0]
    assert child["pool_session_id"] == PARENT and child["pool_run"] == "parent-pool"
    assert child["session_id"] == CHILD and child["create_time"] == 1000


@pytest.mark.parametrize("problem", ["no-parent-binding", "wrong-parent", "no-active-start", "no-birth", "wrong-birth", "expired-grant", "small-grant", "wrong-box", "oversized"])
def test_invalid_parent_pool_refused_without_allocation(pool, problem, capsys):
    directory, bindings, admission, parent = pool
    if problem == "no-parent-binding":
        bindings.pop(PARENT)
    elif problem == "wrong-parent":
        bindings[CHILD]["parent_session_id"] = "not-the-parent"
    elif problem == "no-active-start":
        admission.write_text("")
    elif problem in ("no-birth", "wrong-birth"):
        if problem == "no-birth":
            parent.pop("create_time")
        else:
            parent["create_time"] = 9999
        admission.write_text(json.dumps(parent) + "\n")
    elif problem in ("expired-grant", "small-grant", "wrong-box"):
        path = directory / "slot-grants" / HOLDER / f"{PARENT}.parent-pool.json"
        record = json.loads(path.read_text())
        if problem == "expired-grant":
            record["expires_at"] = slot_grant._iso(time.time() - 1)
        elif problem == "small-grant":
            record["declared_peak_mb"] = 4096
        else:
            record["box_id"] = "other-box"
        path.write_text(json.dumps(record))
    before = admission.read_bytes()
    assert child_run(directory, "one", 101, peak="9" if problem == "oversized" else "4") == 4
    assert admission.read_bytes() == before
    assert "refused" in capsys.readouterr().err


def test_existing_child_recheck_is_idempotent_and_cannot_shrink_or_change_pid(pool, capsys):
    directory, _, admission, _ = pool
    assert child_run(directory, "one", 101) == 0
    assert child_run(directory, "one", 101) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["reservation_selection"]["allocated_peak_mb"] == 4096
    before = admission.read_bytes()
    assert child_run(directory, "one", 101, peak="2") == 4
    assert "shrink" in capsys.readouterr().err
    assert child_run(directory, "one", 999) == 4
    assert "different active PID" in capsys.readouterr().err
    assert admission.read_bytes() == before


def test_selected_release_frees_own_allocation_even_after_parent_grant_expired(pool):
    directory, _, admission, _ = pool
    assert child_run(directory, "one", 101) == 0
    path = directory / "slot-grants" / HOLDER / f"{PARENT}.parent-pool.json"
    path.unlink()
    assert run(directory, "--run", "one", "--command-key", "child", "--release-pid", "101") == 0
    starts, _ = host._ledger_starts(admission)
    assert (CHILD, "one") not in starts and (PARENT, "parent-pool") in starts


def test_parallel_child_allocations_cannot_oversubscribe_parent(pool):
    directory, _, admission, _ = pool
    status = host.MemoryStatus(64 * host.GIB, 64 * host.GIB, host.GIB, 128 * host.GIB)
    def worker(number):
        try:
            verdict, _, _ = host.admit_selected_reservation(admission, 4, status,
                lambda: slot_grant.parent_pool_admission_selector(CHILD, f"child-{number}", "parent-pool", 4096),
                key="child", pid=number, rss_fn=lambda p: 0, alive_fn=lambda p: True,
                create_time_fn=lambda p: 1000, image_fn=lambda p: "python.exe")
            return verdict.admit
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=3) as workers:
        assert sorted(workers.map(worker, [101, 102, 103])) == [False, True, True]


def test_grant_ceiling_does_not_expand_registered_parent_pool(pool, capsys):
    directory, _, admission, _ = pool
    grant(directory, sid=PARENT, run="parent-pool", gib=16)
    before = admission.read_bytes()
    assert child_run(directory, "oversized", 101, peak="9") == 4
    assert "registered pool peak" in capsys.readouterr().err
    assert admission.read_bytes() == before


def test_selected_own_registration_and_release_use_actual_session_run(env):
    grant(env)
    admission = env / "box/admission.jsonl"
    assert run(env, "--admit-peak", "6", "--run", "mine", "--command-key", "pool", "--register-pid", "555") == 0
    row = json.loads(admission.read_text().splitlines()[-1])
    assert (row["session_id"], row["run"], row["pids"], row["create_time"]) == (SID, "mine", [555], 1000)
    (env / "slot-grants" / HOLDER / f"{SID}.mine.json").unlink()
    assert run(env, "--run", "mine", "--command-key", "pool", "--release-pid", "555") == 0
    row = json.loads(admission.read_text().splitlines()[-1])
    assert (row["phase"], row["session_id"], row["run"]) == ("END", SID, "mine")
