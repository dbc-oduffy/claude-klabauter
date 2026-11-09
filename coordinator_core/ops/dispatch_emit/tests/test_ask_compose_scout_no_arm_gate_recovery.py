"""ask_compose: a sized-in-run scout that assembled cleanly but left the gate unanswered gets its arm derived from
route+tshirt, and only an unmapped pair runs the gate agent once."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RECOVERY_MARK = "_scouted?.exit_code === 0 && !_gate.arm && !_gate.halt"
_GATE_MARK = "if (_gate.halt || !_gate.arm)"


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _recovery_block(script: str) -> str:
    return script.split("// no-arm-recovery:begin\n", 1)[1].split("\n  // no-arm-recovery:end", 1)[0]


def _gate_line(script: str) -> str:
    return next(ln for ln in script.splitlines() if _GATE_MARK in ln)


def _write_sizing(tmp_path, route: str, tshirt: str) -> str:
    path = tmp_path / "state" / "sizings" / f"{route}-{tshirt}.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"route: {route}\nestimate:\n  tshirt: {tshirt}\n", encoding="utf-8")
    return str(path)


def _run(
    script: str, tmp_path, *, sizing_path: str, scouted: dict, agent_reply: dict | None, writes: list | None = None
) -> dict:
    """Runs the composed recovery block and the no-arm halt line with `agent` stubbed as the gate agent,
    which answers `agent_reply` and counts its calls."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    block = _recovery_block(script)
    stubs = "".join(f"const {n} = '';\n" for n in sorted(set(re.findall(r"\b_PF\w+", block))))
    if "_refireCmd" in _gate_line(script):
        stubs += "const _refireCmd = () => 'refire';\n"
    if "_chainParamsQ" in block:
        stubs += "const _chainParamsQ = () => '{}';\n"
    prog = (
        stubs
        + "const _agentDefsClause = () => ''; const _RUN_TOKEN = null; const _SESSION_ID = 's'; const REPO_ROOT = 'R';\n"
        + f"const _crossRepoApproved = false; let _gated = []; let _writes = {json.dumps(writes or [])};\n"
        + f"const _sizingRel = {json.dumps(sizing_path)}; const _runId = 'run-1'; const _pmExit = null;\n"
        + "let _calls = 0; let _halted = null; let _adopted = [];\n"
        + "const _adoptStageReply = async (r) => { _adopted.push(r ?? null); };\n"
        + f"const _reply = {json.dumps(agent_reply)};\n"
        + "const agent = async () => { _calls += 1; return _reply; };\n"
        + f"const _scouted = {json.dumps(scouted)};\n"
        + "let _gate = { arm: _scouted.arm ?? null, halt: _scouted.halt ?? null, tshirt: _scouted.tshirt, route: _scouted.route };\n"
        + "(async () => {\n"
        + block
        + "\n"
        + _gate_line(script)
        + "\nconsole.log(JSON.stringify({ calls: _calls, gate: _gate, halted: _halted, adopted: _adopted }));\n})();\n"
    )
    f = tmp_path / "t.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return json.loads(out)


_CLEAN_SCOUT = {"exit_code": 0, "stdout": '{"route": "dispatch"}', "stderr": ""}
# Pairs the derivation does not map (an M plan, an XS-route sizing at S): these still run the agent once.
_UNMAPPED = [("plan", "M"), ("dispatch", "S"), ("spec-dispatch", "XS")]


def _scout_of(route: str, tshirt: str) -> dict:
    return {**_CLEAN_SCOUT, "stdout": json.dumps({"route": route}), "route": route, "tshirt": tshirt}


def test_the_recovery_is_composed_only_when_sized_in_run():
    assert _RECOVERY_MARK in _compose()
    assert _RECOVERY_MARK not in _compose(prompt=None, sizing_rel="state/sizings/x.yaml")


@pytest.mark.parametrize(
    "route, tshirt, arm, writes",
    [("dispatch", "XS", "xs", ["a.py"]), ("spec-dispatch", "S", "s", [])],
)
def test_a_clean_scout_with_no_arm_and_no_halt_derives_the_arm_the_real_gate_answers(
    route, tshirt, arm, writes, tmp_path
):
    """The derived arm skips the gate agent (its baton falls back to the static one; its stage fold
    declines into the stage's own leg) and equals the arm the real gate op answers for that sizing."""
    sizing = _write_sizing(tmp_path, route, tshirt)
    result = _run(
        _compose(), tmp_path, sizing_path=sizing, scouted=_scout_of(route, tshirt),
        agent_reply={"arm": "m_plus", "halt": None}, writes=writes,
    )
    assert result["calls"] == 0 and result["adopted"] == []
    assert result["gate"]["arm"] == arm and result["gate"]["halt"] is None and result["gate"]["derived"] is True
    assert result["halted"] is None
    real = _gate_reply(tmp_path / "real", route, tshirt, **({"writes": writes} if writes else {}))
    assert real["halt"] is None and real["arm"] == result["gate"]["arm"]


def test_an_xs_scout_with_no_footprint_still_runs_the_agent(tmp_path):
    sizing = _write_sizing(tmp_path, "dispatch", "XS")
    reply = {"arm": "xs", "halt": None, "tshirt": "XS", "route": "dispatch", "stage_reply": {"rows": []}}
    result = _run(_compose(), tmp_path, sizing_path=sizing, scouted=_scout_of("dispatch", "XS"), agent_reply=reply)
    assert result["calls"] == 1 and result["adopted"] == [{"rows": []}]
    assert result["gate"]["arm"] == "xs" and result["gate"].get("derived") is None


@pytest.mark.parametrize("route, tshirt", _UNMAPPED)
def test_an_unmapped_route_still_adopts_the_gate_agents_arm(route, tshirt, tmp_path):
    sizing = _write_sizing(tmp_path, route, tshirt)
    reply = {"arm": "M_PLUS", "halt": None, "tshirt": tshirt, "route": route}
    result = _run(
        _compose(), tmp_path, sizing_path=sizing, scouted=_scout_of(route, tshirt), agent_reply=reply, writes=["a.py"]
    )
    assert result["calls"] == 1
    assert result["gate"]["arm"] == "m_plus" and result["gate"]["halt"] is None
    assert result["halted"] is None


@pytest.mark.parametrize("route, tshirt", _UNMAPPED)
def test_a_clean_scout_with_the_gate_agent_halting_adopts_the_named_halt(route, tshirt, tmp_path):
    sizing = _write_sizing(tmp_path, route, tshirt)
    reply = {"arm": None, "halt": {"kind": "room", "reason": "no room"}, "tshirt": tshirt, "route": route}
    result = _run(_compose(), tmp_path, sizing_path=sizing, scouted=_scout_of(route, tshirt), agent_reply=reply)
    assert result["calls"] == 1
    assert result["halted"]["halted"] == "room" and result["halted"]["reason"] == "no room"


@pytest.mark.parametrize("route, tshirt", _UNMAPPED)
@pytest.mark.parametrize("reply", [None, {"arm": None, "halt": None}])
def test_a_gate_agent_that_also_answers_nothing_halts_without_a_refire_into_the_scout(route, tshirt, reply, tmp_path):
    sizing = _write_sizing(tmp_path, route, tshirt)
    result = _run(_compose(), tmp_path, sizing_path=sizing, scouted=_scout_of(route, tshirt), agent_reply=reply)
    assert result["calls"] == 1
    halted = result["halted"]
    assert halted["halted"] == "refusal"
    assert "the gate agent also returned no arm and no halt" in halted["reason"]
    assert sizing in halted["reason"]
    assert "refire to re-run the gate" not in halted["reason"]


@pytest.mark.parametrize(
    "scouted",
    [
        {**_CLEAN_SCOUT, "arm": "xs"},
        {**_CLEAN_SCOUT, "halt": {"kind": "room", "reason": "no room"}},
        {"exit_code": 1, "stdout": "boom", "stderr": "E"},
    ],
)
def test_the_gate_agent_does_not_run_when_the_scout_answered_or_failed(scouted, tmp_path):
    sizing = _write_sizing(tmp_path, "dispatch", "XS")
    reply = {"arm": "s", "halt": None}
    result = _run(_compose(), tmp_path, sizing_path=sizing, scouted=scouted, agent_reply=reply, writes=["a.py"])
    assert result["calls"] == 0


def test_the_unrecovered_reason_still_refires_when_the_gate_was_never_recovered():
    line = _gate_line(_compose())
    assert "refire to re-run the gate" in line and "_gate.recovered" in line


def _gate_reply(tmp_path, route: str, tshirt: str, **params) -> dict:
    """The real gate op over a tmp_path git repo holding a sizing of `route`/`tshirt`."""
    import yaml

    from coordinator_core.ops.dispatch_emit import ask_gate

    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    rel = "state/sizings/x.yaml"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "name": "x", "intent": "fix a thing",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": route, "detents": [], "fork": None,
        "xl_exit": None, "status": "routed",
        "premise": {"provenance": "read", "evidence": "Read the file; the fix is one test."},
        "exit_criterion": {"statement": "the test passes"}, "interaction_mode": "ceo",
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return ask_gate._handler({"sizing_path": rel, **params}, repo_root=tmp_path)


@pytest.mark.parametrize("route, tshirt, params, arm", [
    ("dispatch", "XS", {"writes": ["a.py"]}, "xs"),
    ("spec-dispatch", "S", {}, "s"),
])
def test_the_real_gate_answers_an_arm_for_each_dispatch_route(route, tshirt, params, arm, tmp_path):
    out = _gate_reply(tmp_path, route, tshirt, **params)
    assert out["halt"] is None, out
    assert out["arm"] == arm


def test_the_real_gate_names_the_missing_footprint_on_xs_instead_of_answering_nothing(tmp_path):
    out = _gate_reply(tmp_path, "dispatch", "XS")
    assert out["arm"] is None
    assert out["halt"]["kind"] == "refusal" and "XS needs `writes`" in out["halt"]["reason"]
