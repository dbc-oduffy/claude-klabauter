"""The 3672d0 incident: a ledger resume whose manifest rows are empty dispatched the ledger-remaining leg with
no declared writes. The composed script now takes the leg's writes, refusal and skip from `remaining_leg`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_REFUSAL_HEAD
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_all_landed_refire_e2e import (
    _BLITZ_FN,
    _LANDED,
    _MET,
    _NODE_PROGRAM,
    _PRIOR_RUN,
    _RUN,
    _TOKEN,
    _example_game_repo_shape,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import agent_gate_reply, readers_cfg
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/2026-10-22-65af6a-2-orphan-spines-kept-forever.yaml"
WRITES_HEAD = "Your writes are exactly these declared paths: "


def _run(root, leg) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    requirements_ledger.start(root, _PRIOR_RUN)
    requirements_ledger.revise(root, _PRIOR_RUN, note="finish the owed sizing update")
    requirements_ledger.start(root, _RUN, resumed_from=_PRIOR_RUN)
    reply = agent_gate_reply(root, _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=root))
    ledger = reply["resume_ledger"]
    ledger["revise_notes"] = ["finish the owed sizing update"]
    ledger["remaining"] = {"R1": {"remaining": [f"update {SIZING}"], "evidence": ""}}
    if leg is not None:
        ledger["remaining_leg"] = leg
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id=_RUN, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    cfg = {
        "token": _TOKEN, "verify": _MET, "commit": _LANDED, **readers_cfg(root),
        "replies": {"gate": reply, "gate-stage": reply, "ledger-remaining": {"summary": "ok"}},
    }
    proc = subprocess.run(
        [node, "-"], input="globalThis.log = () => {};\n" + _NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_leg_prompt_lists_the_remaining_paths_the_ledger_named(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _run(repo, {"action": "dispatch", "writes": [SIZING], "unwritable": [], "reason": ""})
    legs = [c for c in out["calls"] if c["label"] == "ledger-remaining"]
    assert len(legs) == 1
    assert WRITES_HEAD + SIZING + "." in legs[0]["prompt"]


def test_a_refused_leg_halts_before_any_ledger_remaining_dispatch(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _run(repo, {"action": "refuse", "writes": [], "unwritable": ["x.py"], "reason": "names no landable path"})
    labels = [c["label"] for c in out["calls"]]
    halted = out["result"]["halted_by"]
    assert "ledger-remaining" not in labels
    assert halted["halted"] == "refusal", halted
    assert halted["reason"] == LEG_REFUSAL_HEAD + "names no landable path"


def test_a_skipped_leg_dispatches_nothing_and_reaches_verify(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _run(repo, {"action": "skip", "writes": [], "unwritable": [], "reason": "re-ruling-only revise"})
    labels = [c["label"] for c in out["calls"]]
    assert "ledger-remaining" not in labels
    assert "verify" in labels
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
