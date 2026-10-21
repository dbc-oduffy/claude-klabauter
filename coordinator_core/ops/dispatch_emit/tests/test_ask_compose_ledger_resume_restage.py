"""A resume_ledger gate reply carries the gate op's own stage of the resumed plan as `manifest_handle`; the fold
adopts it with no restage leg, reaches the ledger check and the terminal commit with no plan author,
executor or review wave, and a halt there lands its halt commit. The whole composed script runs under
node with every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
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
    "test_targets": ["t.py"], "marker_path": "m", "head_sha": "c" * 40,
}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "T6", "status": "met", "wired": True}, {"id": "R2", "status": "met", "wired": True}]}
_UNMET = {"items": [{"id": "T6", "status": "not_met", "essential": True, "remaining": ["still red"]}]}
_NODE_PROGRAM = WORKFLOW_FAKE_JS + r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
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


def _script(monkeypatch, **over) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _run(script: str, *, verify=_MET, commit=_LANDED, manifest=_DISK, gate=None, replies=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = gate or {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME,
        "tshirt": "M", "route": "spec-dispatch", "stage_reply": stage_reply(manifest),
    }
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": commit, **readers_cfg(),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply, **(replies or {})},
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


def test_a_resume_ledger_reply_carrying_the_gate_staged_manifest_reaches_the_commit_with_no_stage_leg(monkeypatch):
    out = _run(_script(monkeypatch))
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    labels = _labels(out)

    stages = [c for c in out["calls"] if OP_ASK_STAGE in c["prompt"] and c["label"] not in {"gate", "gate-stage"}]
    assert stages == [], labels
    assert not {"manifest-read", "ledger-restage", "ready-restage", "run-base"} & set(labels)

    assert labels.index("verify") < labels.index("commit")
    assert res["plan"] == _PLAN and res["commit"]["committed"] is True
    assert OP_NAME in next(c for c in out["calls"] if c["label"] == "commit")["prompt"]
    assert res["halt_commit"] is None


def test_the_resume_path_composes_no_plan_author_executor_or_review_wave(monkeypatch):
    out = _run(_script(monkeypatch))
    labels = _labels(out)
    assert "plan" not in labels and "verify" in labels
    assert not [lab for lab in labels if lab and (lab.startswith(("C1", "review", "test")) or lab == "exec")], labels
    assert not [c for c in out["calls"] if "b/C1.md" in c["prompt"]]


def test_a_stage_that_returns_an_error_still_halts_without_dropping_the_halt_commit(monkeypatch):
    out = _run(_script(monkeypatch), manifest={"error": "boom"})
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
    out = _run(_script(monkeypatch), manifest={**_DISK, "rows": []})
    assert out["result"]["halted_by"] is None
    assert _labels(out).index("verify") < _labels(out).index("commit")


def test_the_no_folded_manifest_and_wholeness_halts_stay_for_every_arm(monkeypatch):
    script = _script(monkeypatch)
    assert "if (!_stagedManifest) { _manifest = { error: 'no folded stage manifest" in script
    assert "'stage manifest relay is not whole: ' + _relayGap" in script
    out = _run(script, manifest={k: v for k, v in _DISK.items() if k != "review_declared_paths"})
    assert out["result"]["halted_by"]["reason"] == "stage manifest relay is not whole: review_declared_paths"
    assert "verify" not in _labels(out)


_OWED = {**_RESUME, "remaining": {"T6": {"remaining": ["still red"]}}}
_OWES_GATE = {
    "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _OWED, "tshirt": "M",
    "route": "spec-dispatch", "stage_reply": stage_reply(_DISK),
}


def test_a_resume_that_owes_work_stages_through_the_replan_agent_over_the_rows_it_added(monkeypatch):
    out = _run(_script(monkeypatch), gate=_OWES_GATE, replies={
        "ledger-replan": {"plan_rel": _PLAN, "added_rows": [], "stage_reply": None},
    })
    calls = [c for c in out["calls"] if c["label"] == "ledger-replan"]
    assert len(calls) == 1
    assert OP_ASK_STAGE in calls[0]["prompt"] and "only_rows" in calls[0]["prompt"]
    assert not {"manifest-read", "ledger-restage"} & set(_labels(out))
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]


def test_a_replan_that_added_rows_but_returned_no_manifest_halts(monkeypatch):
    out = _run(_script(monkeypatch), gate=_OWES_GATE, replies={
        "ledger-replan": {"plan_rel": _PLAN, "added_rows": ["R9"], "stage_reply": None},
    })
    assert out["result"]["halted_by"]["reason"].startswith("no folded stage manifest")


def test_the_no_folded_manifest_halt_names_the_refire_as_its_resume_route(monkeypatch):
    out = _run(_script(monkeypatch), gate=_OWES_GATE, replies={
        "ledger-replan": {"plan_rel": _PLAN, "added_rows": ["R9"], "stage_reply": None},
    })
    halted = out["result"]["halted_by"]
    assert halted["reason"].startswith("no folded stage manifest")
    assert "Refire: " in halted["next_action"] and _SIZING in halted["next_action"]
    assert "coordinator:engine-ask" in halted["next_action"]


def test_a_non_manifest_refusal_carries_no_resume_next_action(monkeypatch):
    out = _run(_script(monkeypatch), manifest={"error": "boom"})
    assert "next_action" not in out["result"]["halted_by"]


def test_the_stage_op_runs_against_the_resume_ledger_plan_in_the_script_text(monkeypatch):
    script = _script(monkeypatch)
    assert f"_gate.{ask_compose.RESUME_LEDGER_KEY}.plan" in script[script.index("const _resumeLedger = "):]


def test_the_no_rows_refusal_is_exempted_only_by_the_resume_ledger_flag(monkeypatch):
    script = _script(monkeypatch)
    assert "!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) { _halted = { halted: " in script
    assert "'stage staged no rows'" in script


def _s_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _s_gate(resume_ledger, manifest=_DISK) -> dict:
    return {
        "arm": "s", "halt": None, "baton": None, "resume_ledger": resume_ledger, "resume_plan": None,
        "tshirt": "S", "route": "spec-dispatch", "stage_reply": stage_reply(manifest),
    }


def test_an_s_arm_ledger_refire_that_owes_work_never_authors_a_new_plan(monkeypatch):
    out = _run(_s_script(monkeypatch), gate=_s_gate(_OWED), replies={
        "ledger-replan": {"plan_rel": _PLAN, "added_rows": ["R9"], "stage_reply": stage_reply(_DISK)},
    })
    labels = _labels(out)
    assert "plan" not in labels, labels
    assert "work:C1" in labels, labels
    assert "no folded stage manifest" not in str(out["result"]["halted_by"])
    assert out["result"]["plan"] == _PLAN


def test_an_s_arm_ledger_refire_that_owes_nothing_reaches_the_commit(monkeypatch):
    out = _run(_s_script(monkeypatch), gate=_s_gate(_RESUME))
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert "plan" not in _labels(out)
    assert _labels(out).index("verify") < _labels(out).index("commit")
