"""Incident pin: a nested launcher child receives no `args` and may not call `workflow()`, so the launcher starts an
XS child a second time through the engine-written staged twin of its script.

The launcher `fan_launcher.write_launcher` renders runs under node with a top-level `workflow(spec)` that loads
`spec.scriptPath` and runs it with `args` undefined; inside every child `workflow()` records the call and throws the
runtime's nesting refusal. The slot line and the twin path are spelled out here, not imported."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, fan_launcher, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (
    _BLITZ_FN,
    _LANDED,
    _MET,
    _RESUME,
    _SIZING,
    big_manifest,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NESTING = "workflow() cannot be called from within a child workflow — nesting is limited to one level"
_RUN_ID = "run-1"
_SLOT = "const _STAGED_INLINE = null;"
_SHA_MISMATCH = (
    "stage reply sidecar does not match its handle: reply text sha256 does not match the handle"
)

_PROGRAM = r"""
const fs = require('fs');
const cfg = JSON.parse(process.env.CFG);
const launcherSrc = fs.readFileSync(process.env.LAUNCHER_FILE, 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const strip = (s) => s.replace(/^export const meta/m, 'const meta');
const topCalls = [];
const childWorkflowCalls = [];
const childResults = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  if (label === 'emit-' + cfg.run_id) return cfg.emit;
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const parallel = async (xs) => Promise.all(xs.map((f) => f()));
const noop = async () => ({});
const childWorkflow = async (spec) => {
  childWorkflowCalls.push(String(spec && spec.scriptPath));
  throw new Error(cfg.nesting);
};
const topWorkflow = async (spec) => {
  topCalls.push({ scriptPath: String(spec.scriptPath), argsUndefined: spec.args === undefined });
  const body = strip(fs.readFileSync(spec.scriptPath, 'utf8'));
  const child = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', body);
  const result = await child(undefined, agent, parallel, noop, () => {}, childWorkflow);
  childResults.push(result);
  return result;
};
const launcher = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', strip(launcherSrc));
launcher(undefined, agent, parallel, noop, () => {}, topWorkflow)
  .then((result) => console.log(JSON.stringify({ result, topCalls, childWorkflowCalls, childResults })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _child_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id=_RUN_ID, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path=f"scratch/warp/{_RUN_ID}.workflow.mjs", in_run_commit=True, launcher_child=True, **REVIEW_KW,
    )


def _stage(root: Path, monkeypatch) -> dict:
    """Lay down the emitted child script, then its manifest sidecar; return the pieces the harness needs."""
    script = _child_script(monkeypatch)
    script_path = root / f"scratch/warp/{_RUN_ID}.workflow.mjs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8", newline="")
    manifest = big_manifest(3)
    handle = reply_sidecar.write_reply(root, root / f"state/warp/{_RUN_ID}/manifest.json", manifest)
    twin = root / f"scratch/warp/{_RUN_ID}.staged-{handle['sha256']}.workflow.mjs"
    return {"script": script, "script_path": script_path, "manifest": manifest, "handle": handle, "twin": twin}


def _launcher(root: Path) -> Path:
    child = fan_launcher.LaunchChild(
        run_id=_RUN_ID, sizing_rel=_SIZING, emit_params={"ask": True, "sizing_path": _SIZING, "reserved_run_id": _RUN_ID},
    )
    return fan_launcher.write_launcher(root, "ask-parent", [child], "sess-1")


def _run(root: Path, staged: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {
        "arm": "xs", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "XS",
        "route": "spec-dispatch", "stage_reply": {"manifest_handle": staged["handle"]},
    }
    cfg = {
        "run_id": _RUN_ID, "nesting": _NESTING, "verify": {"items": _MET["items"]}, "commit": _LANDED,
        "emit": {"path": staged["script_path"].as_posix(), "run_id": _RUN_ID, "fire_args": {"repoRoot": root.as_posix()}},
        "replies": {"gate": gate, "gate-stage": gate},
    }
    proc = subprocess.run(
        [node, "-"], input=_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "LAUNCHER_FILE": payload_file(_launcher(root).read_text(encoding="utf-8")), "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_launcher_starts_the_staged_twin_and_the_twin_adopts_the_manifest(tmp_path, monkeypatch):
    staged = _stage(tmp_path, monkeypatch)
    out = _run(tmp_path, staged)
    run = out["result"]["runs"][0]
    assert run["status"] == "ok", run
    assert out["childResults"][0] == {"stage_pending": {"handle": staged["handle"]}}
    assert [c["scriptPath"] for c in out["topCalls"]] == [staged["script_path"].as_posix(), staged["twin"].as_posix()]
    assert run["result"].get("halted_by") is None, run["result"]
    assert run["result"]["rows"] == [r["id"] for r in staged["manifest"]["rows"]]


def test_no_child_calls_workflow_and_no_child_start_carries_args(tmp_path, monkeypatch):
    out = _run(tmp_path, _stage(tmp_path, monkeypatch))
    assert out["childWorkflowCalls"] == []
    assert out["topCalls"] and all(c["argsUndefined"] for c in out["topCalls"])


def test_a_twin_with_one_inlined_byte_changed_halts_on_the_sha_check(tmp_path, monkeypatch):
    staged = _stage(tmp_path, monkeypatch)
    twin_text = staged["twin"].read_text(encoding="utf-8")
    assert _SLOT not in twin_text
    flipped = "c" * 40
    assert flipped in twin_text
    staged["twin"].write_text(twin_text.replace(flipped, "c" * 39 + "d", 1), encoding="utf-8", newline="")
    out = _run(tmp_path, staged)
    reason = out["result"]["runs"][0]["result"]["halted_by"]["reason"]
    assert _SHA_MISMATCH in reason
    assert out["childWorkflowCalls"] == []


def test_the_emitted_script_holds_the_slot_once_and_the_twin_is_that_script_with_the_slot_filled(tmp_path, monkeypatch):
    staged = _stage(tmp_path, monkeypatch)
    assert staged["script"].count(_SLOT) == 1
    assert "workflow(" not in staged["script"]
    twin_text = staged["twin"].read_text(encoding="utf-8")
    head, tail = staged["script"].split(_SLOT)
    assert twin_text.startswith(head) and twin_text.endswith(tail)


def test_writing_the_sidecar_twice_for_the_same_content_yields_the_same_handle_and_twin(tmp_path, monkeypatch):
    staged = _stage(tmp_path, monkeypatch)
    first = staged["twin"].read_bytes()
    again = reply_sidecar.write_reply(
        tmp_path, tmp_path / f"state/warp/{_RUN_ID}/manifest.json", staged["manifest"],
    )
    assert again == staged["handle"]
    assert staged["twin"].read_bytes() == first
