"""A co_tenant_wait whose predecessor halts staged_stall (a live, escrowed token under a written marker) is refired or released and its EM notified."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import halted_run, run_token, stage_wait, staged_stall, stranded_wait
from coordinator_core.ops.dispatch_emit import co_tenant_owner
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG, record_dir, write_run_stage_at

pytestmark = pytest.mark.complete

WAITER = "ask-20261128T122443-ae8747"
OWNER = "ask-20261128T110536-745e6a"
OWNER_SIZING = "state/sizings/owner.yaml"
WAITER_SIZING = "state/sizings/waiter.yaml"
BOUND = staged_stall.STAGED_STALL_BOUND_SECS


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(staged_stall, "fire_in_flight", lambda root, run_id, cutoff: False)
    monkeypatch.setattr(staged_stall, "fire_progress_epoch", lambda root, run_id: None)
    monkeypatch.setattr(halted_run, "_index_call", lambda *a, **k: None)
    monkeypatch.setattr(
        staged_stall.live_run_index, "read", lambda root, ms, **kw: {OWNER: frozenset()}
    )
    return root


def _staged_owner(root: Path, *, escrow: bool = True) -> Path:
    token = run_token.mint(root, OWNER, "o.mjs", marker=True, session_id="owner-sess", sizing_rel=OWNER_SIZING)
    if escrow:
        assert run_token.escrow(root, OWNER, token)
    rdir = record_dir(root, OWNER)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": OWNER_SIZING}), encoding="utf-8")
    write_run_stage_at(rdir, "staged", session_id="owner-sess")
    old = time.time() - (BOUND + 300)
    for path in [rdir, *rdir.iterdir()]:
        os.utime(path, (old, old))
    return rdir


def _park_waiter(root: Path) -> None:
    stage_wait.park(root, WAITER, [OWNER], [], session_id="waiter-sess", sizing_rel=WAITER_SIZING, reason="co-tenant-wait")


def _spooled(root: Path) -> list:
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def test_the_idle_tick_that_halts_the_owner_spools_its_refire_and_keeps_the_waiter_parked(repo, monkeypatch) -> None:
    _staged_owner(repo)
    _park_waiter(repo)
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: "handed-off")

    assert staged_stall.on_idle_tick([repo], time.time()) is True

    marker = json.loads((record_dir(repo, OWNER) / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["halted"] == staged_stall.HALT_STAGED_STALL
    refires = [a for a in _spooled(repo) if a[RESUME_RUN_ID_ARG] == OWNER]
    assert [a["sizing"] for a in refires] == [OWNER_SIZING]
    assert stage_wait.read(repo, WAITER)["started"] is False


def test_the_waiters_em_session_is_notified_with_the_owner_and_the_action(repo, monkeypatch) -> None:
    _staged_owner(repo)
    _park_waiter(repo)
    sent: list = []
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: sent.append((target, text)) or "handed-off")

    staged_stall.halt_stalled(repo, time.time())

    to_waiter = [text for target, text in sent if target == "waiter-sess"]
    assert len(to_waiter) == 1
    assert WAITER in to_waiter[0] and OWNER in to_waiter[0] and "refire" in to_waiter[0]
    assert [target for target, _ in sent].count("owner-sess") == 1


def test_a_second_halt_pass_neither_refires_nor_notifies_the_waiter_again(repo, monkeypatch) -> None:
    _staged_owner(repo)
    _park_waiter(repo)
    sent: list = []
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: sent.append((target, text)) or "handed-off")
    staged_stall.halt_stalled(repo, time.time())

    assert staged_stall.halt_stalled(repo, time.time()) == []

    assert len([a for a in _spooled(repo) if a[RESUME_RUN_ID_ARG] == OWNER]) == 1
    assert len([t for t, _ in sent if t == "waiter-sess"]) == 1


def test_a_stall_halt_with_nobody_waiting_notifies_only_the_owners_session(repo, monkeypatch) -> None:
    _staged_owner(repo)
    sent: list = []
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: sent.append((target, text)) or "handed-off")

    assert staged_stall.halt_stalled(repo, time.time()) == [OWNER]

    assert [t for t, _ in sent] == ["owner-sess"] and _spooled(repo) == []


def test_the_idle_tick_sweep_alone_refires_a_staged_stall_owner_that_keeps_its_escrow(repo) -> None:
    _staged_owner(repo)
    _park_waiter(repo)
    halted_run.write_halt_marker(
        record_dir(repo, OWNER), halted=staged_stall.HALT_STAGED_STALL, sizing_rel=OWNER_SIZING, reason="idle"
    )

    acts = stranded_wait.sweep(repo, now=time.time())

    assert [(a["action"], a["owner"]) for a in acts] == [("owner-refire", OWNER)]


def test_a_waiter_never_parks_behind_a_staged_stall_owner_that_keeps_its_escrow(repo) -> None:
    _staged_owner(repo)
    halted_run.write_halt_marker(
        record_dir(repo, OWNER), halted=staged_stall.HALT_STAGED_STALL, sizing_rel=OWNER_SIZING, reason="idle"
    )

    assert co_tenant_owner._launcherless_halted(repo, OWNER) is True


@pytest.mark.parametrize(
    "marker,expected",
    [
        ({"status": "halted", "halted": "co_tenant_wait"}, False),
        ({"status": "halted", "halted": "staged_stall"}, True),
        ({"status": "superseded"}, True),
        ({"status": "resumed", "halted": "staged_stall", "retracted_at": "t"}, False),
    ],
    ids=["escrowed-co-tenant-wait-still-resumes", "escrowed-staged-stall", "escrowed-superseded", "retracted-stall"],
)
def test_an_escrowed_owner_is_non_resuming_only_for_a_halt_nothing_resumes(repo, marker, expected) -> None:
    rdir = _staged_owner(repo)
    (rdir / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")

    assert co_tenant_owner._launcherless_halted(repo, OWNER) is expected
