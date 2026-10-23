"""Incident pin: an XS-arm child the multi-ask launcher starts never calls `workflow()`.

The runtime refuses a second workflow nesting level, so a launcher child that reached the stage reader halted with
"stage reply sidecar does not match its handle". Every case runs the composed script under node with a `workflow`
global that records the call and throws that refusal; the launcher-child script must never reach it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGED_INLINE_SLOT
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401  (stage_root is an autouse fixture)
    _BLITZ_FN,
    _LANDED,
    _MET,
    _NODE_PROGRAM,
    _RESUME,
    _SIZING,
    _TOKEN,
    big_manifest,
    readers_cfg,
    sidecar_text,
    stage_reply,
    stage_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NESTING = "workflow() cannot be called from within a child workflow — nesting is limited to one level"

_NESTING_REFUSING_PROGRAM = _NODE_PROGRAM.replace(
    "const workflow = makeWorkflow(cfg);",
    "const workflow = async (spec) => { workflowCalls.push(String(spec && spec.scriptPath)); "
    "throw new Error(cfg.nesting); };",
)


def _script(monkeypatch, **extra) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW, **extra,
    )


def _run(script: str, reply: dict, *, args: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "xs", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "XS",
        "route": "spec-dispatch", "stage_reply": reply,
    }
    cfg = {
        "token": _TOKEN, "verify": {"items": _MET["items"]}, "commit": _LANDED, "nesting": _NESTING,
        "replies": {"gate": gate, "gate-stage": gate}, **readers_cfg(),
        **({"args": args} if args is not None else {}),
    }
    proc = subprocess.run(
        [node, "-"], input=_NESTING_REFUSING_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_launcher_child_without_args_returns_stage_pending_and_records_no_workflow_call(monkeypatch):
    reply = stage_reply(big_manifest(3))
    out = _run(_script(monkeypatch, launcher_child=True), reply)
    assert out["result"] == {"stage_pending": {"handle": reply["manifest_handle"]}}
    assert out["workflowCalls"] == []


def test_a_launcher_child_whose_slot_holds_the_staged_manifest_adopts_it_with_no_workflow_call(monkeypatch):
    manifest = big_manifest(3)
    reply = stage_reply(manifest)
    script = _script(monkeypatch, launcher_child=True)
    assert script.count(STAGED_INLINE_SLOT) == 1
    inline = json.dumps({"handle": reply["manifest_handle"], "text": sidecar_text(manifest)})
    script = script.replace(STAGED_INLINE_SLOT, "const _STAGED_INLINE = " + inline + ";")
    out = _run(script, reply)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert out["result"]["rows"] == [r["id"] for r in manifest["rows"]]
    assert out["workflowCalls"] == []


def test_the_same_compose_without_launcher_child_still_reads_the_reader_through_workflow(monkeypatch):
    reply = stage_reply(big_manifest(3))
    script = _script(monkeypatch)
    assert "workflow({ scriptPath: reader })" in script
    out = _run(script, reply)
    assert len(out["workflowCalls"]) == 1
    assert out["result"]["halted_by"]["reason"].startswith("stage reply sidecar does not match its handle: ")
    assert _NESTING in out["result"]["halted_by"]["reason"]
