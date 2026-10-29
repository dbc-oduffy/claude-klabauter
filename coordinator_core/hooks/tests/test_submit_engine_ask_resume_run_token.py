"""A stored resume script is fired with a run token bound to the path its commit leg presents, so
the resumed run commits in-run; a mint that cannot bind leaves the fire as it was."""

from __future__ import annotations

import json
import re

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, SIZING_REL, STORED_REL, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

COMMIT_LEG_SCRIPT = f"{RUN_DIR_ROOT}/{HALTED_ID}.workflow.mjs"


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
