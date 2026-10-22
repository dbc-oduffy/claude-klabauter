"""An all-landed resume_ledger (kind all-landed, zero-row manifest) is judged against the landed plan
at HEAD, runs no executor or review leg, and a not_met judge lands the ordinary ledger-check halt.
The whole composed script runs under node with every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALTED_RECORD_PARAM,
    RESUME_LEDGER_KIND_FIELD,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-10-18-landed.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_RESUME = {
    "run_id": "none", "plan": _PLAN, "blocking": ["R1"], RESUME_LEDGER_KIND_FIELD: ALL_LANDED_HALT_KIND,
    "halt_sha": None, "remaining": {},
}
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": [], "review_declared_paths": [],
    "test_targets": [], "marker_path": "m", "head_sha": "c" * 40,
}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_UNMET = {"items": [{"id": "R1", "status": "not_met", "essential": True, "remaining": ["still red"]}]}
_NODE_PROGRAM = WORKFLOW_FAKE_JS + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, *, verify=_MET, resume=_RESUME) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": resume,
        "tshirt": "M", "route": "spec-dispatch", "stage_reply": stage_reply(_DISK),
    }
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": _LANDED, **readers_cfg(),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def test_an_all_landed_resume_with_a_zero_row_manifest_reaches_verify_then_commit_with_no_executor(monkeypatch):
    out = _run(_script(monkeypatch))
    res = out["result"]
    labels = _labels(out)
    assert res["halted_by"] is None, res["halted_by"]
    assert labels.index("verify") < labels.index("commit")
    assert not {"plan", "ledger-remaining", "ledger-replan", "exec"} & set(labels), labels
    assert not [lab for lab in labels if lab and lab.startswith(("review", "test"))], labels
    assert res["commit"]["committed"] is True


def test_the_all_landed_judge_prompt_omits_the_halt_diff_clause(monkeypatch):
    verify = next(c for c in _run(_script(monkeypatch))["calls"] if c["label"] == "verify")["prompt"]
    assert "Judge only these requirement rows, now, against the landed work of the plan at HEAD: R1" in verify
    assert "changed since the halt commit" not in verify
    assert "the diff the halt already judged is not_met" not in verify
    assert "a remaining-work leg then ran" not in verify


def test_a_ledger_check_resume_keeps_the_halt_diff_clause(monkeypatch):
    resume = {k: v for k, v in _RESUME.items() if k != RESUME_LEDGER_KIND_FIELD}
    resume.update(run_id="ask-prior", halt_sha="a" * 40)
    out = _run(_script(monkeypatch), resume=resume)
    verify = next(c for c in out["calls"] if c["label"] == "verify")["prompt"]
    assert "met only on work changed since the halt commit " + "a" * 40 in verify
    assert "against the current tree: R1" in verify
    assert "ledger-remaining" in _labels(out)


def test_a_not_met_verdict_on_an_all_landed_resume_lands_the_ledger_check_halt_commit(monkeypatch):
    out = _run(_script(monkeypatch), verify=_UNMET)
    res = out["result"]
    labels = _labels(out)
    assert res["halted_by"]["halted"] == "ledger-check"
    assert "no folded stage manifest" not in json.dumps(res["halted_by"])
    assert labels.index("verify") < labels.index("halt-commit")
    assert "commit" not in labels and "ledger-remaining" not in labels
    halt = next(c for c in out["calls"] if c["label"] == "halt-commit")
    assert HALTED_RECORD_PARAM in halt["prompt"] and "ledger-check" in halt["prompt"]
    assert res["halt_commit"]["committed"] is True
