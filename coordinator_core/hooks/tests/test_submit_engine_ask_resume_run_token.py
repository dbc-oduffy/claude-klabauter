"""A stored resume script is fired with a run token bound to the path its commit leg presents, so
the resumed run commits in-run; a mint that cannot bind leaves the fire as it was. A resume through
the launcher leaves the run holding a live escrowed token for its staged twin."""

from __future__ import annotations

import json
import re

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, PLAN_REL, SIZING_REL, STORED_REL, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

COMMIT_LEG_SCRIPT = f"{RUN_DIR_ROOT}/{HALTED_ID}.workflow.mjs"
LANDED = {"sha": "d" * 40, "rows": [], "paths": []}


def test_stored_script_comes_with_a_token_the_commit_leg_is_admitted_by(tmp_path, seams):
    root = _repo(tmp_path)
    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None)
    assert env is None and result["path"] == str(root / STORED_REL)
    args = result["fire_args"]
    assert args["repoRoot"] == root.as_posix()
    assert re.fullmatch(r"[0-9a-f]{32}", args["runToken"])
    assert run_token.admits(root, HALTED_ID, args["runToken"], COMMIT_LEG_SCRIPT)
    assert not run_token.admits(root, HALTED_ID, args["runToken"], "scratch/warp/other.workflow.mjs")


def test_the_token_reaches_the_launch_args_and_never_the_printed_reply(tmp_path, seams):
    _, _, emit = seams
    root = _repo(tmp_path)
    env = mod.evaluate(_payload(root), emit=emit)
    out = _hso(env)
    token = out["updatedInput"]["args"]["runToken"]
    assert re.fullmatch(r"[0-9a-f]{32}", token)
    assert token not in json.dumps({k: v for k, v in out.items() if k != "updatedInput"})
    assert token not in (root / STORED_REL).read_text(encoding="utf-8")


def test_a_mint_that_cannot_bind_fires_the_script_without_a_token(tmp_path, seams, monkeypatch):
    def refuse(*a, **k):
        raise ValueError("no git common dir")

    monkeypatch.setattr(run_token, "mint", refuse)
    root = _repo(tmp_path)
    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None)
    assert env is None and result["path"] == str(root / STORED_REL)
    assert "fire_args" not in result


def _launcher_resume(root, escrow_in_launch: bool = False):
    """`_resume_route` down the launcher branch (no stored record); the launch seam writes no token
    unless `escrow_in_launch`, as a deferred child emit writes none."""
    def launch(r, parent_id, children, session_id):
        if escrow_in_launch:
            token = run_token.mint(r, HALTED_ID, COMMIT_LEG_SCRIPT, carry_prior=True)
            run_token.escrow(r, HALTED_ID, token)
        return r / "scratch" / "launcher.workflow.mjs"

    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", launch)
    assert result is None and env is not None
    return run_token.escrowed(root, HALTED_ID)


def _halt_again(root):
    write_halt_marker(
        root / RECORD_DIR_ROOT / HALTED_ID, halted="review_blocked", sizing_rel=SIZING_REL, plan_rel=PLAN_REL,
        run_base_sha="a" * 40,
    )


def test_a_launcher_resume_leaves_a_live_escrowed_token_bound_to_the_commit_leg(tmp_path, seams):
    root = _repo(tmp_path, record=False)
    token = _launcher_resume(root)
    assert token is not None and run_token.admits(root, HALTED_ID, token, COMMIT_LEG_SCRIPT)
    assert not run_token.admits(root, HALTED_ID, token, "scratch/warp/other.workflow.mjs")


def test_a_token_the_launch_already_escrowed_is_left_alone(tmp_path, seams):
    root = _repo(tmp_path, record=False)
    first = _launcher_resume(root, escrow_in_launch=True)
    assert first is not None
    again = run_token.escrowed(root, HALTED_ID)
    assert again == first


def test_a_launcher_refire_after_a_landed_halt_commit_lands_its_second_halt_commit(tmp_path, seams):
    root = _repo(tmp_path, record=False)
    spent = _launcher_resume(root)
    assert run_token.redeem(root, HALTED_ID, spent, COMMIT_LEG_SCRIPT)
    run_token.consume(root, HALTED_ID, landed=LANDED)
    _halt_again(root)
    assert not run_token.admits(root, HALTED_ID, spent, COMMIT_LEG_SCRIPT)

    fresh = _launcher_resume(root)
    assert fresh is not None and fresh != spent
    assert run_token.redeem(root, HALTED_ID, fresh, COMMIT_LEG_SCRIPT)
    run_token.consume(root, HALTED_ID, landed=LANDED)
    assert not run_token.redeem(root, HALTED_ID, fresh, COMMIT_LEG_SCRIPT)


def test_a_stored_script_refire_after_a_landed_halt_commit_lands_its_second_halt_commit(tmp_path, seams):
    root = _repo(tmp_path)
    _, first = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None)
    spent = first["fire_args"]["runToken"]
    assert run_token.redeem(root, HALTED_ID, spent, COMMIT_LEG_SCRIPT)
    run_token.consume(root, HALTED_ID, landed=LANDED)
    _halt_again(root)

    _, second = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None)
    fresh = second["fire_args"]["runToken"]
    assert fresh != spent
    assert run_token.redeem(root, HALTED_ID, fresh, COMMIT_LEG_SCRIPT)


def test_a_stalled_mint_on_a_launcher_resume_detaches_and_still_answers_the_envelope(tmp_path, seams, monkeypatch):
    import threading

    root = _repo(tmp_path, record=False)
    release = threading.Event()
    monkeypatch.setattr(mod, "_emit_join_s", lambda: 0.05)
    monkeypatch.setattr(run_token, "mint", lambda *a, **k: release.wait(5) or None)
    try:
        env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", lambda r, p, c, s: r / "scratch" / "l.workflow.mjs")
    finally:
        release.set()
    assert result is None and env is not None
    assert run_token.escrowed(root, HALTED_ID) is None
