"""A resume_ledger gate reply that carries no manifest re-stages the resumed plan at the fold, reaches the
ledger check and the terminal commit with no plan author, executor or review wave, and a halt there lands
its halt commit. The whole composed script runs under node with every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-10-16-resumed.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_RESUME = {
    "run_id": "ask-prior", "plan": _PLAN, "blocking": ["T6", "R2"], "em_discharged": {"V1": "done by the EM"},
}
_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
    "test_targets": ["t.py"], "marker_path": "m",
}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "T6", "status": "met", "wired": True}, {"id": "R2", "status": "met", "wired": True}]}
_UNMET = {"items": [{"id": "T6", "status": "not_met", "essential": True, "remaining": ["still red"]}]}
_NODE_PROGRAM = r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (label === 'manifest-read') return cfg.disk;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  if (String(prompt).includes(cfg.stageOp) && cfg.stageReply !== null) return cfg.stageReply;
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {})
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _script(monkeypatch, **over) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _run(script: str, *, verify=_MET, stage_reply="default", commit=_LANDED, disk=_DISK, gate=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = gate or {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME,
        "tshirt": "M", "route": "spec-dispatch",
    }
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": commit, "disk": disk, "stageOp": OP_ASK_STAGE,
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
        "stageReply": {"manifest": disk} if stage_reply == "default" else stage_reply,
    }
    proc = subprocess.run(
        [node, "-e", _NODE_PROGRAM], capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC": script, "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


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
        env={**os.environ, "SRC": _script(monkeypatch)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr


def test_a_resume_ledger_reply_with_no_manifest_restages_the_resumed_plan_and_reaches_the_commit(monkeypatch):
    out = _run(_script(monkeypatch))
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    labels = _labels(out)

    stages = [c for c in out["calls"] if OP_ASK_STAGE in c["prompt"] and c["label"] not in {"gate", "gate-stage"}]
    assert len(stages) == 1 and _PLAN in stages[0]["prompt"], labels

    assert "manifest-read" in labels
    assert labels.index("manifest-read") < labels.index("verify") < labels.index("commit")
    assert res["plan"] == _PLAN and res["commit"]["committed"] is True
    assert "dispatch.terminal_commit" in next(c for c in out["calls"] if c["label"] == "commit")["prompt"]
    assert res["halt_commit"] is None


def test_the_resume_path_composes_no_plan_author_executor_or_review_wave(monkeypatch):
    out = _run(_script(monkeypatch))
    labels = _labels(out)
    assert "plan" not in labels and "verify" in labels
    assert not [lab for lab in labels if lab and (lab.startswith(("C1", "review", "test")) or lab == "exec")], labels
    assert not [c for c in out["calls"] if "b/C1.md" in c["prompt"]]


def test_a_stage_that_returns_an_error_still_halts_without_dropping_the_halt_commit(monkeypatch):
    out = _run(_script(monkeypatch), stage_reply={"manifest": {"error": "boom"}})
    res = out["result"]
    assert res["halted_by"]["kind"] == "refusal"
    assert "boom" in res["halted_by"]["reason"]
    assert "verify" not in _labels(out)
    assert _labels(out).count("halt-commit") == 1
    assert res["halt_commit"]["committed"] is True


def test_a_halt_on_the_resume_ledger_path_lands_its_halt_commit(monkeypatch):
    out = _run(_script(monkeypatch), verify=_UNMET)
    res = out["result"]
    assert res["halted_by"]["halted"] == "ledger-check"
    labels = _labels(out)
    assert labels.index("verify") < labels.index("halt-commit")
    assert "commit" not in labels
    halt = next(c for c in out["calls"] if c["label"] == "halt-commit")
    assert HALTED_RECORD_PARAM in halt["prompt"] and "ledger-check" in halt["prompt"]
    assert res["halt_commit"]["committed"] is True


def test_a_resume_whose_stage_staged_no_rows_still_reaches_the_ledger_check(monkeypatch):
    out = _run(_script(monkeypatch), disk={**_DISK, "rows": []})
    assert out["result"]["halted_by"] is None
    assert _labels(out).index("verify") < _labels(out).index("commit")


def test_the_no_folded_manifest_and_wholeness_halts_stay_for_every_arm(monkeypatch):
    script = _script(monkeypatch)
    assert "if (!_stagedManifest) { _manifest = { error: 'no folded stage manifest" in script
    assert "stage manifest on disk is not whole: " in script
    out = _run(script, disk={k: v for k, v in _DISK.items() if k != "review_declared_paths"})
    assert out["result"]["halted_by"]["reason"] == "stage manifest on disk is not whole: review_declared_paths"
    assert "verify" not in _labels(out)


def test_the_stage_op_runs_against_the_resume_ledger_plan_in_the_script_text(monkeypatch):
    script = _script(monkeypatch)
    assert f"_gate.{ask_compose.RESUME_LEDGER_KEY}.plan" in script[script.index("const _resumeLedger = "):]


def test_the_no_rows_refusal_is_exempted_only_by_the_resume_ledger_flag(monkeypatch):
    script = _script(monkeypatch)
    assert "!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger)) { _halted = { halted: " in script
    assert "'stage staged no rows'" in script
