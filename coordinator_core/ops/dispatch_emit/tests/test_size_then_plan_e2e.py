"""The size-then-plan script runs end to end under node on a fixture repo, its agents stubbed with real
sizing-assemble and ask_gate outputs, and the command it hands the assemble agent is run for real."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from coordinator_core import sizing_assemble
from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import NO_PLAN_HAND_OFF_FLAG, PM_EXIT_QUESTION_KEY, STOP_AFTER_PLAN, parked_next_action
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

PLAN_PATH = "docs/plans/2026-10-12-fixture.md"

_BLITZ_FN = (
    "  async function planBlitz(args) {\n"
    "    const p = await agent('blitz', { label: 'blitz', phase: 'plan' });\n"
    "    return { ready: [p.plan_rel] };\n"
    "  }"
)

_HARNESS = r"""
const _seen = { phases: [], labels: [], prompts: {} };
const phase = (name) => { _seen.phases.push(name); };
const _stub = JSON.parse(process.env.STUB);
const agent = async (prompt, opts) => {
  _seen.labels.push(opts.label);
  _seen.prompts[opts.label] = prompt;
  if (!(opts.label in _stub)) throw new Error('unstubbed agent label ' + opts.label);
  return _stub[opts.label];
};
(async () => {
%(script)s
})().then(
  (result) => { console.log(JSON.stringify({ result, seen: _seen })); },
  (e) => { console.error(String(e && e.stack || e)); process.exit(1); },
);
"""


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _wrap_stage(_text):
    return _BLITZ_FN, ["Size", "Plan"]


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_wrap_stage,
        plan_blitz_text="stub",
        stop_after=STOP_AFTER_PLAN,
        **REVIEW_KW,
    )


def _run_script(script: str, stub: dict) -> dict:
    body = script.replace("export const meta", "const meta", 1)
    proc = subprocess.run(
        [_node(), "-"],
        input=_HARNESS % {"script": body},
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "STUB": json.dumps(stub)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _make_repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    return root


def _scout(tshirt: str, flags: list[str]) -> dict:
    return {
        "tshirt": tshirt, "intent": "add a thing", "name": "fixture-thing",
        "premise_provenance": "read", "premise_evidence": "read it",
        "exit_criterion": "the thing exists", "writes": ["src/thing.py"], "gated": [],
        "jtbd_unclear": "--jtbd-unclear" in flags,
    }


def _sizing_args(scout: dict, write: str, flags: list[str]) -> list[str]:
    return [
        "--tshirt", scout["tshirt"], "--intent", scout["intent"], "--name", scout["name"],
        "--premise-provenance", scout["premise_provenance"], "--premise-evidence", scout["premise_evidence"],
        "--exit-criterion", scout["exit_criterion"], *flags, "--write", write, "--json", NO_PLAN_HAND_OFF_FLAG,
    ]


def _assemble(repo: Path, argv: list[str], monkeypatch, mode: str, capsys) -> dict:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sizing_assemble, "_resolve_interaction_mode_and_source", lambda _v: (mode, "flag"))
    monkeypatch.setattr(sizing_assemble, "_open_routed_obligation", lambda _p: None)
    capsys.readouterr()
    code = sizing_assemble.main(argv)
    out, _err = capsys.readouterr()
    assert code == 0, _err
    return json.loads(out)


def _fixture(tmp_path: Path, monkeypatch, capsys, tshirt: str, mode: str, flags: list[str]):
    """Real assemble + real gate in one repo; a second repo for replaying the script's own command."""
    scout = _scout(tshirt, flags)
    rel = f"state/sizings/{date.today().isoformat()}-{scout['name']}-1.yaml"
    repo = _make_repo(tmp_path / "captured")
    decision = _assemble(repo, _sizing_args(scout, rel, flags), monkeypatch, mode, capsys)
    verdict = ask_gate.gate(repo, rel, writes=scout["writes"]).to_json()
    return scout, decision, verdict, rel


def _stub(scout: dict, decision: dict, verdict: dict) -> dict:
    return {
        "size": {
            **scout,
            "exit_code": 0, "stdout": json.dumps(decision, indent=2, sort_keys=True), "stderr": "",
            **verdict, "tshirt": decision["resolved_estimate"]["tshirt"], "route": decision["route"],
        },
        "blitz": {"plan_rel": PLAN_PATH},
    }


def _replayed_decision(seen: dict, repo: Path, monkeypatch, mode: str, capsys, scout: dict) -> dict:
    """Fill the scout prompt's assemble template the way the scout would, then run it for real."""
    prompt = seen["prompts"]["size"]
    start = prompt.index('"${COORDINATOR_SETTINGS_HOME')
    cmd = prompt[start:prompt.index(NO_PLAN_HAND_OFF_FLAG, start) + len(NO_PLAN_HAND_OFF_FLAG)]
    q = shlex.quote
    cmd = cmd.replace("<tshirt>", q(scout["tshirt"])).replace("<intent>", q(scout["intent"]))
    cmd = cmd.replace("<premise_provenance>", q(scout["premise_provenance"]))
    cmd = cmd.replace("<premise_evidence>", q(scout["premise_evidence"]))
    cmd = cmd.replace("<exit_criterion>", q(scout["exit_criterion"])).replace("<name>", scout["name"])
    cmd = cmd.replace(
        "[--jtbd-unclear when jtbd_unclear is true]", "--jtbd-unclear" if scout["jtbd_unclear"] else ""
    ).replace("[--well-trodden-step-change when well_trodden_step_change is true]", "")
    cmd = cmd.replace(
        "[--research-class/-appetite/-source/-depth/-target/-question from research when research is returned] ", ""
    ).replace("[--design-route <design_route> when design_route is returned] ", "")
    assert "'REPO/state/sizings/" in cmd, cmd
    cmd = cmd.replace("'REPO/state/", f"'{repo.as_posix()}/state/")
    assert "<" not in cmd, cmd
    argv = shlex.split(cmd, posix=True)
    assert argv[0].endswith("/bin/sizing-assemble")
    return _assemble(repo, argv[1:], monkeypatch, mode, capsys)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_m_plan_route_runs_scout_through_plan_in_one_script(tmp_path, monkeypatch, capsys):
    scout, decision, verdict, _rel = _fixture(tmp_path, monkeypatch, capsys, "M", "ceo", [])
    assert verdict["halt"] is None, verdict
    out = _run_script(_script(), _stub(scout, decision, verdict))
    halted = out["result"]["halted_by"]
    assert halted["halted"] == "parked" and halted["plan"] == PLAN_PATH
    assert halted["next_action"] == parked_next_action(PLAN_PATH)
    assert "planned" not in out["result"]
    assert out["seen"]["labels"] == ["size", "blitz"]
    assert [p for p in out["seen"]["phases"] if p in ("size", "gate", "plan")] == ["size", "gate", "plan"]
    assert not {"stage", "execute", "review"} & set(out["seen"]["phases"])
    replayed = _replayed_decision(out["seen"], _make_repo(tmp_path / "replayed"), monkeypatch, "ceo", capsys, scout)
    assert replayed == {**decision, "write": replayed["write"]}
    assert Path(replayed["write"]["path"]).name == Path(decision["write"]["path"]).name


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize(
    "tshirt,flags",
    [("XL", []), ("XL", ["--jtbd-unclear"])],
    ids=["xl-pm-decision", "shape"],
)
def test_pm_decision_and_shape_halt_with_the_decisions_pm_exit_question(tmp_path, monkeypatch, capsys, tshirt, flags):
    scout, decision, verdict, _rel = _fixture(tmp_path, monkeypatch, capsys, tshirt, "hands-on", flags)
    question = decision[PM_EXIT_QUESTION_KEY]
    assert question and (verdict["halt"] is not None or verdict["arm"] is None), verdict
    out = _run_script(_script(), _stub(scout, decision, verdict))
    halted = out["result"]["halted_by"]
    assert halted[PM_EXIT_QUESTION_KEY] == question
    assert "planned" not in out["result"]
    assert "plan" not in out["seen"]["phases"] and "blitz" not in out["seen"]["labels"]
    replayed = _replayed_decision(
        out["seen"], _make_repo(tmp_path / "replayed"), monkeypatch, "hands-on", capsys, scout
    )
    assert replayed == {**decision, "write": replayed["write"]}
    assert replayed["route"] == ("shape" if "--jtbd-unclear" in flags else decision["route"])
    assert replayed[PM_EXIT_QUESTION_KEY] == question


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_failed_assemble_halts_before_plan_and_spawns_only_the_scout():
    stub = {"size": {**_scout("M", []), "exit_code": 2, "stdout": "boom", "stderr": "bad flag"}}
    out = _run_script(_script(), stub)
    halted = out["result"]["halted_by"]
    assert halted["halted"] == "refusal" and "sizing-assemble failed (exit 2)" in halted["reason"]
    assert out["seen"]["labels"] == ["size"]
    assert "planned" not in out["result"]
