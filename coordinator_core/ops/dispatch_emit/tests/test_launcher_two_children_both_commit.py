"""Two launcher children under one parent each keep their own run token through a stage-wait wake to commit.

Incident: a child parked behind its sibling's overlapping writes lost its escrowed token when the
sibling's landing woke it, so its staged twin was tokenless and its commit stage halted "no run token"."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import fan_launcher, reply_sidecar, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import big_manifest
from coordinator_core.ops.dispatch_emit.tests.test_launcher_child_stage_no_token_leg_incident import (
    _emit_child,
    _twin_inline,
)
from coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring import env  # noqa: F401


def _spooled(root: Path) -> list:
    from coordinator_core.ask_spool import record

    return list(record.list_pending(str(root)))


def _stage_twin_token(root: Path, run_id: str) -> str | None:
    handle = reply_sidecar.write_reply(root, root / f"state/warp/{run_id}/manifest.json", big_manifest(3))
    twin = root / fan_launcher.staged_twin_rel(run_id, handle["sha256"])
    return _twin_inline(twin).get("runToken")


def _commit(root: Path, run_id: str, token: str) -> None:
    script = f"scratch/warp/{run_id}.workflow.mjs"
    assert run_token.admits(root, run_id, token, script)
    assert run_token.redeem(root, run_id, token, script)
    run_token.consume(root, run_id, landed={"sha": "a" * 40, "rows": [], "paths": []})
    assert run_token.spent_receipt(root, run_id, token, script) is not None


def test_both_launcher_children_reach_commit_with_their_own_token_after_a_stage_wait_wake(env, monkeypatch):  # noqa: F811
    root = env.root
    first = _emit_child(env, monkeypatch)
    second = _emit_child(env, monkeypatch)
    blocker, waiter = first["run_id"], second["run_id"]
    assert blocker != waiter
    tokens = {rid: run_token.escrowed(root, rid) for rid in (blocker, waiter)}
    assert all(t is not None for t in tokens.values()) and tokens[blocker] != tokens[waiter]

    stage_wait.park(root, waiter, [blocker], ["a.py"], session_id="sess-1")
    assert _stage_twin_token(root, blocker) == tokens[blocker]
    _commit(root, blocker, tokens[blocker])

    assert stage_wait.read(root, waiter)["started"] is True
    assert run_token.escrowed(root, waiter) == tokens[waiter]
    assert _spooled(root) == []
    assert _stage_twin_token(root, waiter) == tokens[waiter]
    _commit(root, waiter, tokens[waiter])


def test_a_woken_waiter_without_escrow_is_still_retired_and_refired(env, monkeypatch):  # noqa: F811
    root = env.root
    run_token.mint(root, "blocker", "scratch/warp/blocker.workflow.mjs", session_id="peer")
    run_token.mint(root, "waiter", "scratch/warp/waiter.workflow.mjs", session_id="s1", sizing_rel="state/sizings/x.yaml")
    stage_wait.park(root, "waiter", ["blocker"], [], session_id="s1")
    run_token.consume(root, "blocker")
    assert not run_token.is_live(root, "waiter")
    assert len(_spooled(root)) == 1
