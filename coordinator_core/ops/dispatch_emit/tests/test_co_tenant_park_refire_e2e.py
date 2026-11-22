"""End to end: a default `--export-run` co-tenant refusal parks behind the live owner, and the owner's end spools the refire."""
from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import live_run_index, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG
from coordinator_core.ops.dispatch_emit.tests.test_clean_overlay_co_tenant_park import (  # noqa: F401
    OWNER, PEER, WAITER, _export, _isolated, _repo, _typed,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "docs/sizing/waiter.yaml"


def _mint_live_pair(root):
    run_token.mint(root, WAITER, "w.mjs", marker=True, session_id="waiter-sess", sizing_rel=SIZING)
    run_token.mint(root, OWNER, "o.mjs", marker=True, session_id="owner-sess")
    live_run_index.stage(root, OWNER, [PEER], time.time())


def _spooled_args(root):
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def test_live_owner_parks_then_its_consume_spools_one_refire_with_the_recorded_sizing(tmp_path, capsys):
    root = _repo(tmp_path)
    _mint_live_pair(root)

    assert _export(root) == 2

    line = _typed(capsys)
    assert line["parked"] == "co-tenant-wait" and line["owner"] == OWNER and line["module"] == "pkg.peer"
    record = stage_wait.read(root, WAITER)
    assert record["blockers"] == [OWNER] and record["sizing_rel"] == SIZING
    assert _spooled_args(root) == []

    run_token.consume(root, OWNER)

    (args,) = _spooled_args(root)
    assert args["sizing"] == SIZING and args[RESUME_RUN_ID_ARG] == WAITER
    assert (stage_wait.read(root, WAITER) or {}).get("blockers") == []


def test_the_refire_survives_the_waiters_token_being_discarded_by_its_halt(tmp_path, capsys):
    root = _repo(tmp_path)
    _mint_live_pair(root)
    _export(root)
    capsys.readouterr()
    run_token.discard(root, WAITER)

    run_token.consume(root, OWNER)

    (args,) = _spooled_args(root)
    assert args["sizing"] == SIZING and args[RESUME_RUN_ID_ARG] == WAITER


def test_halted_owner_is_a_typed_cause_naming_its_refire_and_nothing_is_spooled(tmp_path, capsys):
    root = _repo(tmp_path)
    run_token.mint(root, WAITER, "w.mjs", marker=True, session_id="waiter-sess", sizing_rel=SIZING)
    run_dir = root / "state" / "warp" / OWNER
    run_dir.mkdir(parents=True)
    (run_dir / "halted.json").write_text(json.dumps({
        "status": "halted", "run_artifacts": {"withheld": {PEER: "kept"}}, "next_action": {"call": "coordinator-invoke engine-ask OWNER"},
    }), encoding="utf-8")

    assert _export(root) == 2

    line = _typed(capsys)
    assert line["cause"] == "co_tenant_dependency_on_halted_run" and line["owner"] == OWNER
    assert line["owner_refire"] == "coordinator-invoke engine-ask OWNER"
    assert stage_wait.read(root, WAITER) is None
    assert _spooled_args(root) == []
