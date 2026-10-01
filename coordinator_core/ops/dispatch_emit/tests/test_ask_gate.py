"""ask_gate: one test per halt kind, XS/S skip null acceptance, M+ halts on it, accepted M mints a baton."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-01-gate.yaml"
ACCEPTED = {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo, tshirt="S", route="spec-dispatch", accepted=None, mode="pm", **over):
    doc = {
        "schema": "sizing-object",
        "name": "gate fixture",
        "intent": "exercise the gate",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": route,
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": mode,
        "exit_criterion": {"statement": "done", "accepted": accepted},
    }
    doc.update(over)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_room_route_halts_as_room_with_route(repo):
    _put(repo, tshirt="XL", route="shape", accepted={"pm_quote": "y"})
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "room" and v.halt["route"] == "shape"


def test_m_plus_null_acceptance_halts_as_touchpoint_naming_invoke_line(repo):
    _put(repo, tshirt="M", route="plan")
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "touchpoint"
    assert "sizing.accept_exit_criterion" in v.halt["touchpoint"] and REL in v.halt["touchpoint"]


def test_refusals_halt_all_named_at_once(repo):
    _put(repo, tshirt="S", route="dispatch", accepted={"pm_quote": "y"}, status="draft", interaction_mode=None)
    v = gate(repo, REL)
    assert v.halt["kind"] == "refusal"
    for field in ("interaction_mode", "status", "route"):
        assert field in v.halt["reason"]


def test_unknown_tshirt_is_a_refusal(repo):
    _put(repo, tshirt="Q")
    assert gate(repo, REL).halt["kind"] == "refusal"


@pytest.mark.parametrize("tshirt,route,arm", [("S", "spec-dispatch", "s"), ("XS", "dispatch", "xs")])
def test_xs_s_with_null_acceptance_proceeds(repo, tshirt, route, arm):
    _put(repo, tshirt=tshirt, route=route)
    v = gate(repo, REL, writes=["a.py"])
    assert v.halt is None and v.arm == arm and v.baton is None


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_accepted_m_returns_arm_and_baton(repo):
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.baton["path"].startswith("state/handoffs/") and (repo / v.baton["path"]).is_file()
    assert v.to_json()["baton"]["id"] == v.baton["id"]


def _composed_params(op: str, **values) -> dict:
    """The params object the composed script's agent is told to send to `op`, values filled in."""
    import json
    import re

    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

    script = _compose()
    line = next(ln for ln in script.splitlines() if op in ln and "JSON.stringify({" in ln)
    body = re.search(r"JSON\.stringify\(\{ (.*?) \}\)", line).group(1)
    keys = [pair.split(":")[0].strip() for pair in body.split(",")]
    return json.loads(json.dumps({k: values.get(k) for k in keys}))


def test_gate_key_the_composed_script_sends_is_the_key_the_handler_accepts(repo):
    from coordinator_core.ops.dispatch_emit.ask_gate import _handler

    (repo / ".git").mkdir()
    _put(repo, tshirt="S", route="spec-dispatch")
    params = _composed_params("dispatch.ask_gate", sizing_path=REL, writes=[])
    assert "error" not in (reply := _handler(params, repo_root=repo / ".git")), reply
    assert reply["arm"] == "s" and reply["halt"] is None


def test_stage_keys_the_composed_script_sends_are_the_keys_the_handler_reads(repo):
    import inspect

    from coordinator_core.ops.dispatch_emit import ask_stage

    sent = set(_composed_params("dispatch.ask_stage"))
    src = inspect.getsource(ask_stage._handler)
    assert all(f'"{k}"' in src for k in sent), sent


def test_gate_resolves_the_sizing_from_the_common_dir_the_engine_passes(repo):
    """The dispatcher hands common_dir-scoped ops `<repo>/.git`, not the worktree root."""
    from coordinator_core.ops.dispatch_emit.ask_gate import _handler

    (repo / ".git").mkdir()
    _put(repo, tshirt="S", route="spec-dispatch")
    reply = _handler({"sizing_path": REL, "writes": []}, repo_root=repo / ".git")
    assert reply["halt"] is None and reply["arm"] == "s", reply
