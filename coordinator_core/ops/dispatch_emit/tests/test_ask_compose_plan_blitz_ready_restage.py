"""A plan-blitz ready entry carrying neither readyOpReply nor readyOpError is staged by one mechanical
fallback leg and reaches execute; a readyOpError still halts. The composed script runs under node with
every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/2026-10-16-ready.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
    "test_targets": ["t.py"], "marker_path": "m",
}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_GATE = {"arm": "m_plus", "halt": None, "baton": None, "tshirt": "M", "route": "spec-dispatch"}

_NODE_PROGRAM = r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'manifest-read') return cfg.disk;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  if (label === 'ready-restage') return cfg.restage;
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {})
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _blitz_fn(ready: dict) -> str:
    return f"  async function planBlitz(args) {{\n    return {{ ready: [{json.dumps(ready)}] }};\n  }}"


def _script(monkeypatch, ready: dict) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_blitz_fn(ready), ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, *, restage=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    cfg = {
        "token": _TOKEN, "commit": _LANDED, "disk": _DISK,
        "replies": {"gate": _GATE, "gate-stage": _GATE},
        "restage": {"manifest": _DISK} if restage is None else restage,
    }
    proc = subprocess.run(
        [node, "-e", _NODE_PROGRAM], capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC": script, "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _past_stage(out: dict) -> bool:
    """The run adopted the manifest and left the stage fold: any later halt is the stubbed review wave's."""
    halt = out["result"]["halted_by"]
    return halt is None or "manifest" not in json.dumps(halt) and "stage" not in str(halt.get("reason", ""))


def test_the_emitted_script_parses_as_the_workflow_runner_parses_it(monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    program = (
        "const src = process.env.SRC.replace(/^export const meta/m, 'const meta');"
        "new (Object.getPrototypeOf(async function () {}).constructor)("
        "'args', 'agent', 'parallel', 'pipeline', 'phase', src);"
    )
    proc = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "SRC": _script(monkeypatch, {"planPath": _PLAN})}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr


def test_a_ready_entry_without_readyop_reply_is_staged_by_the_fallback_leg_and_reaches_execute(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN}))
    labels = _labels(out)
    assert labels.count("ready-restage") == 1, labels
    restage = next(c for c in out["calls"] if c["label"] == "ready-restage")
    assert OP_ASK_STAGE in restage["prompt"] and _PLAN in restage["prompt"]
    assert "manifest-read" in labels
    assert labels.index("ready-restage") < labels.index("manifest-read")
    assert _past_stage(out), out["result"]["halted_by"]


def test_a_ready_entry_carrying_readyop_reply_is_not_restaged(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpReply": _DISK}))
    assert "ready-restage" not in _labels(out)
    assert _past_stage(out), out["result"]["halted_by"]


def test_plan_blitz_did_not_run_error_is_staged_by_the_fallback_leg(monkeypatch):
    err = "ready op did not run on the leg that made the ready call"
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpError": err}))
    assert _labels(out).count("ready-restage") == 1
    assert _past_stage(out), out["result"]["halted_by"]


def test_a_ready_entry_carrying_readyop_error_still_halts_without_the_fallback(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN, "readyOpError": "boom"}))
    assert "ready-restage" not in _labels(out)
    assert out["result"]["halted_by"]["kind"] == "refusal"
    assert "no folded stage manifest" in out["result"]["halted_by"]["reason"]


def test_a_failed_fallback_leg_halts(monkeypatch):
    out = _run(_script(monkeypatch, {"planPath": _PLAN}), restage={"manifest": {"error": "stage broke"}})
    assert out["result"]["halted_by"]["kind"] == "refusal"
    assert "stage broke" in out["result"]["halted_by"]["reason"]
    assert "C1" not in _labels(out)
