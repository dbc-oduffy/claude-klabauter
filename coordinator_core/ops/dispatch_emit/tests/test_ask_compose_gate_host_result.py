"""A composed ask under a host `engineOp` consumes the engine's gate result directly; without one it keeps the scoped relay.

The whole composed script runs under node at default workflow options, once with an `engineOp` and once without,
over a presized ask and a raw ask. The archived worker outputs below are the two recorded transcription failures."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from coordinator_core.codex_runner import workflow as codex_workflow
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import OP_ASK_GATE, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file

from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_INVOKE = ask_compose._INVOKE

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN_ID = "run-1"
_TOKEN = "0123456789abcdef0123456789abcdef"
_SIZING = "state/sizings/x.yaml"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"

#: Archived worker output, case "s": the engine's reply closed early, then the worker's own tail ran on
#: (json.loads: "Extra data"). The engine's reply is the leading value.
_S_ENGINE_RESULT = {
    "arm": "s", "halt": None, "baton": None, "tshirt": "S", "route": "spec-dispatch",
    "resume_plan": "docs/plans/2026-11/x.md", "sizing_path": "state/sizings/2026-11/x.yaml",
    "derived_rows": ["LR1"],
    "remaining_leg": {"action": "dispatch", "writes": ["a.py", "b.py"], "unwritable": ["c.py"], "em_owed": [], "reason": ""},
    "resume_ledger": {"run_id": "warp-prior", "plan": "docs/plans/2026-11/x.md", "blocking": ["R1"], "unlanded": ["C1"]},
}
_S_WORKER_STDOUT = (
    json.dumps(_S_ENGINE_RESULT)[:-1].rsplit(',"resume_ledger"', 1)[0] + "}"
    + ',"resume_phases":{"reuse":[],"rerun":["gate"],"verdicts":{}},"resume_ready":null}'
)

#: Archived worker output, case "m_plus": the engine's JSON-RPC envelope plus `_execution`, relayed whole,
#: which the worker's schema reads as a reply with no `arm` (halt fb9db7547c).
_M_PLUS_ENGINE_RESULT = {
    "arm": "m_plus", "halt": None,
    "baton": {"id": "hnd-w4-a6d775", "path": "state/handoffs/2026-11/w4.md", "route": "plan", "deliverableId": "dlv-a7a3f0"},
    "acceptance": {"by": "engine-size-rule", "route": "plan", "tshirt": "L", "mode": "ceo"},
    "resume_plan": "docs/plans/w4.md", "resume_rows": ["C0", "C1"], "pending_rulings": [],
    "sizing_path": "state/sizings/2026-11/w4.yaml", "tshirt": "L", "route": "plan",
}
_M_PLUS_WORKER_STDOUT = json.dumps({
    "jsonrpc": "2.0", "id": 1, "result": _M_PLUS_ENGINE_RESULT,
    "_execution": {"exit_code": 0, "params_file": "scratch/ask-gate.json"},
})

_PROGRAM = r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const calls = [];
const ops = [];
const agent = async (prompt, opts) => {
  calls.push({ label: opts && opts.label, options: opts, prompt: String(prompt) });
  if (cfg.replies[opts.label] !== undefined) return cfg.replies[opts.label];
  return {};
};
const engineOp = cfg.host
  ? async (op, params) => {
      ops.push({ op, params });
      const out = cfg.host.replies[Math.min(ops.length, cfg.host.replies.length) - 1];
      if (out && out.__throw) throw new Error(out.__throw);
      return out;
    }
  : undefined;
const workflow = async () => { throw new Error('no stage reader in this fixture'); };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', 'engineOp', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow, engineOp)
  .then((result) => console.log(JSON.stringify({ calls, ops, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""

_UNMAPPED_SCOUT = {
    "exit_code": 0, "stdout": '{"route": "plan"}', "stderr": "", "route": "plan", "tshirt": "M",
    "arm": None, "halt": None,
}


def _script(kind: str, monkeypatch: pytest.MonkeyPatch) -> str:
    if kind == "presized":
        monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
        return compose_ask_script(
            repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id=_RUN_ID, session_id=None,
            wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
            script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
        )
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=_RUN_ID, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", **REVIEW_KW,
    )


def _run(script: str, *, host: list | None, replies: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    cfg = {
        "token": _TOKEN, "replies": replies or {}, "host": {"replies": host} if host is not None else None,
    }
    proc = subprocess.run(
        [node, "-"], input=_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _replies(kind: str, gate: dict | None = None) -> dict:
    return {"size": _UNMAPPED_SCOUT, "size-retry": _UNMAPPED_SCOUT} if kind == "raw" else {"gate": gate or {}}


def _gate_agents(out: dict) -> list[dict]:
    return [c for c in out["calls"] if c["label"] == "gate"]


def _relay_params(prompt: str) -> dict:
    """The JSON object the relay prompt tells the worker to pass to `dispatch.ask_gate`, quote-escapes undone."""
    m = re.search(r"dispatch\.ask_gate '(\{.*?\})'(?: --repo|` )", prompt, re.S)
    assert m, prompt[:400]
    return json.loads(m.group(1).replace("'\"'\"'", "'"))


def _halt_reason(out: dict) -> str:
    return out["result"]["halted_by"]["reason"]


KINDS = ["presized", "raw"]


def test_the_archived_worker_outputs_are_the_recorded_transcription_failures() -> None:
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        json.loads(_S_WORKER_STDOUT)
    value, _ = json.JSONDecoder().raw_decode(_S_WORKER_STDOUT)
    assert value["arm"] == "s"
    assert "arm" not in json.loads(_M_PLUS_WORKER_STDOUT)


@pytest.mark.parametrize("kind", KINDS)
def test_a_host_engine_op_gets_the_gate_with_no_gate_worker(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(_script(kind, monkeypatch), host=[_M_PLUS_ENGINE_RESULT], replies=_replies(kind))
    assert [o["op"] for o in out["ops"]][:1] == [OP_ASK_GATE]
    assert _gate_agents(out) == []
    params = out["ops"][0]["params"]
    assert params["run_id"] == _RUN_ID and params["require_live_token"] is True
    assert params["stage_act"] is True and "gated" in params and "cross_repo_approved" in params
    if kind == "raw":
        assert params["chain_emit"] is True and params["target_root"] == "REPO"
    else:
        assert "chain_emit" not in params, "an embedded plan-blitz owns the chained run"
    assert "halted_by" in out["result"] and "gate-reply-invalid" not in json.dumps(out["result"])


@pytest.mark.parametrize("kind", KINDS)
def test_without_engine_op_the_scoped_relay_runs_with_the_same_params_and_operation_identity(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(kind, monkeypatch)
    host = _run(script, host=[_M_PLUS_ENGINE_RESULT], replies=_replies(kind))
    relay = _run(script, host=None, replies=_replies(kind, _M_PLUS_ENGINE_RESULT))
    assert relay["ops"] == []
    gate = _gate_agents(relay)
    assert len(gate) == 1
    assert gate[0]["options"]["op_id"] == OP_ASK_GATE
    assert "--repo REPO" in gate[0]["prompt"]
    assert _relay_params(gate[0]["prompt"]) == host["ops"][0]["params"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("engine_result", [_S_ENGINE_RESULT, _M_PLUS_ENGINE_RESULT], ids=["arm_s", "arm_m_plus"])
def test_both_archived_outcomes_survive_malformed_worker_text(
    kind: str, engine_result: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    junk = {"arm": None, "halt": {"kind": "gate-reply-invalid", "reason": "'arm' is a required property"}}
    out = _run(_script(kind, monkeypatch), host=[engine_result], replies=_replies(kind, junk))
    assert _gate_agents(out) == []
    assert "gate-reply-invalid" not in json.dumps(out["result"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "bad",
    [
        None, {}, [], "text", {"arm": 3, "halt": None}, {"arm": "m_plus", "halt": "x"},
        {"arm": "s", "halt": None, "run_id": "other-run"},
        {"arm": "s", "halt": None},
        {"arm": "s", "halt": None, "sizing_path": ""},
    ],
    ids=["null", "empty", "array", "string", "arm_type", "halt_type", "other_run", "unbound", "empty_sizing_binding"],
)
def test_a_missing_or_mismatched_host_result_halts_and_is_never_replaced_by_worker_text(
    kind: str, bad, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _run(_script(kind, monkeypatch), host=[bad], replies=_replies(kind, _M_PLUS_ENGINE_RESULT))
    assert _gate_agents(out) == []
    assert "gate-reply-invalid" in json.dumps(out["result"])


@pytest.mark.parametrize("kind", KINDS)
def test_a_run_id_echo_binds_the_result_and_an_engine_halt_passes_through(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    echoed = {**_M_PLUS_ENGINE_RESULT, "sizing_path": None, "run_id": _RUN_ID}
    out = _run(_script(kind, monkeypatch), host=[echoed], replies=_replies(kind))
    assert "gate-reply-invalid" not in json.dumps(out["result"])
    refusal = {"arm": None, "halt": {"kind": "refusal", "reason": "run token spent"}}
    out = _run(_script(kind, monkeypatch), host=[refusal], replies=_replies(kind))
    assert "gate-reply-invalid" not in json.dumps(out["result"]) and "run token spent" in json.dumps(out["result"])


@pytest.mark.parametrize("kind", KINDS)
def test_a_32004_is_rerun_once_then_halts_naming_it(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    err = {"__throw": json.dumps({"code": -32004, "message": "timeout"})}
    out = _run(_script(kind, monkeypatch), host=[err, _M_PLUS_ENGINE_RESULT], replies=_replies(kind))
    assert len([o for o in out["ops"] if o["op"] == OP_ASK_GATE]) == 2
    assert "gate-reply-invalid" not in json.dumps(out["result"])
    out = _run(_script(kind, monkeypatch), host=[err, err], replies=_replies(kind))
    assert len([o for o in out["ops"] if o["op"] == OP_ASK_GATE]) == 2
    assert "-32004" in _halt_reason(out)


@pytest.mark.parametrize("kind", KINDS)
def test_the_fallback_relay_runs_a_claude_child_scoped_to_the_declared_operation(
    kind: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    script = _script(kind, monkeypatch)
    out = _run(script, host=None, replies=_replies(kind, _M_PLUS_ENGINE_RESULT))
    options = _gate_agents(out)[0]["options"]
    seen: list[list[str]] = []

    def fake_run(argv, prompt, *, stdout, **_kw):
        seen.append(list(argv))
        stdout.write(json.dumps({"is_error": False, "structured_output": _M_PLUS_ENGINE_RESULT}).encode())
        return type("Proc", (), {"returncode": 0, "pid": 1})()

    monkeypatch.setattr(codex_workflow, "_run_admitted", fake_run)
    monkeypatch.setattr(codex_workflow, "_save_process", lambda *a, **k: None)
    monkeypatch.setattr(codex_workflow.shutil, "which", lambda *_a, **_k: "claude")
    agent_options = {k: v for k, v in options.items() if k != "agentType"}
    codex_workflow.run_agent("p", agent_options, repo_root=tmp_path, artifact_dir=tmp_path / "a", backend="claude")
    argv = seen[0]
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allowedTools"]
    assert allowed == [f"Bash(coordinator-invoke {OP_ASK_GATE}:*)", f"Bash({_INVOKE} {OP_ASK_GATE}:*)"]


def test_a_claude_child_with_no_declared_operation_gets_no_allowed_tools_rule() -> None:
    assert codex_workflow._operation_allow({"label": "x"}) == []


def test_the_claude_child_for_the_declared_operation_carries_only_its_allowed_tools_rule() -> None:
    assert codex_workflow._operation_allow({"op_id": OP_ASK_GATE}) == [
        "--allowedTools", f"Bash(coordinator-invoke {OP_ASK_GATE}:*)",
        "--allowedTools", f"Bash({_INVOKE} {OP_ASK_GATE}:*)",
    ]
    assert codex_workflow._operation_allow({}) == []
    with pytest.raises(codex_workflow.AgentFailed):
        codex_workflow._operation_allow({"op_id": "dispatch.ask_gate:* Bash"})
