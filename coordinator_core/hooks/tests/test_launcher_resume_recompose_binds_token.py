"""A launcher resume started by recompose leaves the run holding a live escrowed token bound to its commit leg,
whether or not the hook window was spent on the halt-time record read before the launcher was written."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, SIZING_REL, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

COMMIT_LEG_SCRIPT = f"{RUN_DIR_ROOT}/{HALTED_ID}.workflow.mjs"


def _recompose_resume(root: Path, emit) -> dict:
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    return env


def test_recompose_resume_with_the_window_intact_escrows_a_token_for_the_commit_leg(tmp_path, seams):
    spawned, _, emit = seams
    root = _repo(tmp_path, record=False)
    _recompose_resume(root, emit)
    assert spawned == [HALTED_ID]
    token = run_token.escrowed(root, HALTED_ID)
    assert token is not None and run_token.admits(root, HALTED_ID, token, COMMIT_LEG_SCRIPT)


@pytest.mark.deliberate_wall_clock(reason="a record read outliving the hook window spends it; blocked time burns no CPU")
def test_recompose_resume_after_a_stalled_record_read_still_escrows_a_token_for_the_commit_leg(
    tmp_path, seams, monkeypatch
):
    spawned, _, emit = seams
    root = _repo(tmp_path)
    real = hrr._read_file

    def slow(path):
        if Path(path).name == hrr.RESUME_FILE:
            time.sleep(0.5)
        return real(path)

    monkeypatch.setattr(hrr, "_read_file", slow)
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    _recompose_resume(root, emit)
    assert spawned == [HALTED_ID]
    token = run_token.escrowed(root, HALTED_ID)
    assert token is not None, (
        "token null on recompose resume: the stalled record read spent the hook window, "
        "so _ensure_launcher_run_token returned before minting and the run holds no live escrowed token "
        f"(is_live={run_token.is_live(root, HALTED_ID)}) for the {SIZING_REL} resume's commit leg"
    )
    assert run_token.admits(root, HALTED_ID, token, COMMIT_LEG_SCRIPT)
