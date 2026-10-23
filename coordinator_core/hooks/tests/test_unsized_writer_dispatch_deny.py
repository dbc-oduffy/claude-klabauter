"""unsized_writer_dispatch_deny: an unsized top-level writer-type Agent dispatch is refused."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import block_unenumerated_agent_type as gate
from coordinator_core.hooks import nudge_em_code_dispatch as nudge
from coordinator_core.hooks import unsized_writer_dispatch_deny as mod
from coordinator_core.hooks.tests.test_nudge_em_code_dispatch_budget import (  # noqa: F401
    SID,
    _edit,
    _is_deny,
    _reason,
    env,
)
from coordinator_core.session import engine_route_state

WRITERS = ["coordinator:executor", "executor", "coordinator:code-executor", "general-purpose"]


def _payload(subagent_type, prompt="do it", **extra) -> dict:
    ti = {"prompt": prompt}
    if subagent_type is not None:
        ti["subagent_type"] = subagent_type
    return {"tool_name": "Agent", "session_id": SID, "tool_input": ti, **extra}


def _set_sized(monkeypatch, sized: bool) -> None:
    monkeypatch.setattr(engine_route_state, "session_sizing_state", lambda p, s: (sized, False))


def _level(monkeypatch, level: str) -> None:
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: level)


def _hso(out) -> dict:
    return out["hookSpecificOutput"]


@pytest.mark.parametrize("stype", WRITERS)
def test_ac3_unsized_writer_is_denied_with_fire_step(monkeypatch, stype):
    from coordinator_core.warp_fire import fire_step

    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    out = mod.unsized_writer_dispatch_deny(_payload(stype), stype)
    assert _hso(out)["permissionDecision"] == "deny"
    assert _hso(out)["permissionDecisionReason"].endswith(fire_step())


def test_ac3_omitted_type_resolves_to_general_purpose_and_is_denied(monkeypatch):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    out = gate.check(_payload(None))
    assert _hso(out)["permissionDecision"] == "deny"


def test_ac3_warn_level_is_advisory(monkeypatch):
    _level(monkeypatch, "warn")
    _set_sized(monkeypatch, False)
    out = mod.unsized_writer_dispatch_deny(_payload("coordinator:executor"), "coordinator:executor")
    assert out is not None and _hso(out).get("permissionDecision") != "deny"


@pytest.mark.parametrize("stype", WRITERS)
def test_ac4_sized_session_admits(monkeypatch, stype):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, True)
    assert mod.unsized_writer_dispatch_deny(_payload(stype), stype) is None


@pytest.mark.parametrize(
    "stype",
    ["coordinator:test-runner", "coordinator:code-reviewer", "coordinator:plan-author", "Explore"],
)
def test_ac4_other_types_are_not_judged(monkeypatch, stype):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    assert mod.unsized_writer_dispatch_deny(_payload(stype), stype) is None


def test_probe_exception_denies_naming_failure_and_engine_ask(monkeypatch):
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    def boom(p):
        raise RuntimeError("probe")

    _level(monkeypatch, "strict")
    monkeypatch.setattr(sizing, "_repo_root", boom)
    out = mod.unsized_writer_dispatch_deny(_payload("coordinator:executor", cwd="/repo"), "coordinator:executor")
    assert _hso(out)["permissionDecision"] == "deny"
    reason = _hso(out)["permissionDecisionReason"]
    assert "probe failed" in reason and "engine-ask" in reason


def test_normal_sized_path_admits_through_real_probe(monkeypatch):
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    _level(monkeypatch, "strict")
    monkeypatch.setattr(sizing, "_repo_root", lambda p: "/repo")
    monkeypatch.setattr(sizing, "_session_touched_sizing_files", lambda s, r: ["x.yaml"])
    monkeypatch.setattr(sizing, "_load_sizing_object", lambda r, rel: {"route": "none"})
    monkeypatch.setattr(sizing, "_session_touched_plan_files", lambda s, r: [])
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:executor", cwd="/repo"), "coordinator:executor") is None


def test_ac4_subagents_own_dispatch_passes(monkeypatch):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    out = mod.unsized_writer_dispatch_deny(
        _payload("coordinator:executor", agent_id="a1"), "coordinator:executor"
    )
    assert out is None


def test_ac5_unsized_code_handoff_gets_engine_ask_not_executor_text(monkeypatch):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    out = gate.check(_payload("general-purpose", "Edit coordinator_core/foo.py to fix the parser."))
    reason = _hso(out)["permissionDecisionReason"]
    assert "No sizing or engine run" in reason
    assert "dispatch subagent_type coordinator:executor" not in reason


def test_ac8_no_session_read_for_non_writer_and_no_spawn(monkeypatch):
    def nospawn(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", nospawn)

    def boom(*a, **k):
        raise AssertionError("read")

    monkeypatch.setattr(engine_route_state, "session_sizing_state", boom)
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:test-runner"), "coordinator:test-runner") is None
    assert mod.unsized_writer_dispatch_deny({"tool_name": "Edit"}, "coordinator:executor") is None
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:executor", agent_id="x"), "coordinator:executor") is None

    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, False)
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:executor"), "coordinator:executor")


def test_r33_replay(env, monkeypatch):
    root, _ = env
    _level(monkeypatch, "strict")
    monkeypatch.setattr(nudge, "_session_sizing_state", lambda p, s: (False, False))
    _set_sized(monkeypatch, False)

    small = "".join(f"s{i}\n" for i in range(6))
    assert nudge.op(_edit(root, "a.py", "l1\n", "l1\n" + small)) is None
    (root / "a.py").write_text((root / "a.py").read_text().replace("l1\n", "l1\n" + small, 1))
    assert nudge.op(_edit(root, "a.py", "l2\n", "l2\nt1\n")) is None
    (root / "a.py").write_text((root / "a.py").read_text().replace("l2\n", "l2\nt1\n", 1))

    big = "".join(f"x{i}\n" for i in range(100))
    denied = nudge.op(_edit(root, "b.py", "l1\n", big))
    assert _is_deny(denied)
    assert "XS budget spent (projected)" in _reason(denied)

    from coordinator_core.warp_fire import fire_step

    assert fire_step() in _reason(denied)
    out = gate.check(_payload("coordinator:executor", "Apply the change."))
    assert _hso(out)["permissionDecision"] == "deny"
    assert _hso(out)["permissionDecisionReason"].endswith(fire_step())

    _set_sized(monkeypatch, True)
    assert mod.unsized_writer_dispatch_deny(
        _payload("coordinator:executor", "Apply the change."), "coordinator:executor"
    ) is None


def _lane(monkeypatch, rec, run_ids=()):
    from coordinator_core.hooks import flag_em_poll_in_flight as poll
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing
    from coordinator_core.session import xs_lane_budget

    monkeypatch.setattr(poll, "session_workflow_run_ids", lambda s: list(run_ids))
    monkeypatch.setattr(sizing, "_repo_root", lambda p: "/repo")
    monkeypatch.setattr(xs_lane_budget, "read_lane", lambda r, s: rec)


def _xs(denies):
    return {"kind": "xs", "state": "open", "base_sha": "a", "paths": [], "denies": denies}


def _after(monkeypatch, rec, run_ids=(), stype="coordinator:executor"):
    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, True)
    _lane(monkeypatch, rec, run_ids)
    return mod.unsized_writer_dispatch_deny(_payload(stype), stype)


def test_after_budget_deny_denies_with_fire_step(monkeypatch):
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope
    from coordinator_core.warp_fire import fire_step

    out = _after(monkeypatch, _xs(1))
    assert _hso(out)["permissionDecision"] == "deny"
    reason = _hso(out)["permissionDecisionReason"]
    assert reason.endswith(fire_step()) and "XS budget denied" in reason
    assert measure_envelope(out).prose_bytes <= MESSAGE_PROSE_CAP_BYTES


def test_after_budget_deny_with_workflow_run_admits(monkeypatch):
    assert _after(monkeypatch, _xs(1), run_ids=["r1"]) is None


def test_after_budget_deny_residue_record_admits(monkeypatch):
    rec = dict(_xs(2), kind="residue")
    assert _after(monkeypatch, rec) is None


def test_after_budget_deny_zero_denies_or_no_record_admits(monkeypatch):
    assert _after(monkeypatch, _xs(0)) is None
    assert _after(monkeypatch, None) is None


def test_after_budget_deny_read_failure_admits(monkeypatch):
    from coordinator_core.session import xs_lane_budget

    _level(monkeypatch, "strict")
    _set_sized(monkeypatch, True)
    _lane(monkeypatch, _xs(1))

    def boom(r, s):
        raise RuntimeError("x")

    monkeypatch.setattr(xs_lane_budget, "read_lane", boom)
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:executor"), "coordinator:executor") is None


def test_after_budget_deny_non_writer_skips_lane_read_and_spawn(monkeypatch):
    from coordinator_core.session import xs_lane_budget

    def boom(*a, **k):
        raise AssertionError("read")

    monkeypatch.setattr(xs_lane_budget, "read_lane", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _set_sized(monkeypatch, True)
    assert mod.unsized_writer_dispatch_deny(_payload("Explore"), "Explore") is None
    _lane(monkeypatch, _xs(1))
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert mod.unsized_writer_dispatch_deny(_payload("coordinator:executor"), "coordinator:executor") is not None
