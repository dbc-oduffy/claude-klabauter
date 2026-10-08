"""Phase-1 gates: one halt per gate, call order on the all-proceed path, no subprocess."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.plan_chain import contract, phase1_gates as pg

PLAN = "docs/plans/2026-10-08-demo.md"


def _manifest(baton: str = "state/handoffs/b.md") -> contract.ChainManifest:
    return contract.ChainManifest(
        sizing_object="state/sizings/s.yaml", baton=baton, deliverable_id=None,
        interaction_mode="pm", repo_root=".", trail_dir="t", wave_args={}, script_source="x",
    )


@pytest.fixture
def calls(monkeypatch, tmp_path):
    log: list[str] = []
    state = {"stamp": "match", "auth": 0, "roadmap": None, "claim": True}

    def fake_stamp(path, repo_root=None):
        log.append("stamp")
        return 0, {"verdict": state["stamp"], "next_move": "ask PM"}

    def fake_auth(argv):
        log.append("auth")
        assert argv[0] == "authorize-invocation"
        assert "--utterance" not in argv
        assert argv[argv.index("--authorized-by-sizing") + 1].endswith("s.yaml")
        if state["auth"]:
            import sys
            print("sizing not accepted", file=sys.stderr)
        return state["auth"]

    def fake_gate(root, subject=None):
        log.append("roadmap")
        return {"batons": [{"path": subject, "execution_gate": state["roadmap"]}]}

    def fake_claim(slug, cwd=None, *, for_execution=False, plan_path=None):
        log.append("claim")
        assert for_execution and slug == "2026-10-08-demo" and plan_path == PLAN
        if not state["claim"]:
            import sys
            print("held by peer", file=sys.stderr)
        return state["claim"]

    monkeypatch.setattr(pg, "stamp_check", fake_stamp)
    monkeypatch.setattr(pg.exec_auth_stamp, "main", fake_auth)
    monkeypatch.setattr(pg, "assemble_plan_gate", fake_gate)
    monkeypatch.setattr(pg, "claim_plan", fake_claim)
    baton = tmp_path / "state/handoffs/b.md"
    baton.parent.mkdir(parents=True)
    baton.write_text("---\ntitle: b\n---\n", encoding="utf-8")
    state["log"] = log
    state["baton"] = baton
    return state


def _roadmap_baton(state):
    state["baton"].write_text("---\nroadmap_id: r1\n---\n", encoding="utf-8")


def test_all_proceed_in_order_skips_roadmap_for_plain_baton(calls, tmp_path):
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None
    assert calls["log"] == ["stamp", "auth", "claim"]


def test_all_proceed_with_roadmap_baton_open(calls, tmp_path):
    _roadmap_baton(calls)
    calls["roadmap"] = {"open": True, "blocking": []}
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None
    assert calls["log"] == ["stamp", "auth", "roadmap", "claim"]


@pytest.mark.parametrize("verdict", ["match", "stale-bookkeeping", "absent"])
def test_non_substantive_stamp_verdicts_proceed(calls, tmp_path, verdict):
    calls["stamp"] = verdict
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None


def test_stale_substantive_halts_first(calls, tmp_path):
    calls["stamp"] = "stale-substantive"
    h = pg.run(_manifest(), PLAN, repo_root=tmp_path)
    assert h.halted_at == contract.HALTS["stamp-stale-substantive"]
    assert calls["log"] == ["stamp"]


def test_authorize_refusal_carries_stderr(calls, tmp_path):
    calls["auth"] = 2
    h = pg.run(_manifest(), PLAN, repo_root=tmp_path)
    assert h.halted_at == "execute" and "sizing not accepted" in h.reason
    assert calls["log"] == ["stamp", "auth"]


def test_roadmap_gate_shut_halts(calls, tmp_path):
    _roadmap_baton(calls)
    calls["roadmap"] = {"open": False, "blocking": [{"blocker": "other-baton"}]}
    h = pg.run(_manifest(), PLAN, repo_root=tmp_path)
    assert h.halted_at == contract.HALTS["roadmap-gate-shut"] and "other-baton" in h.reason
    assert calls["log"] == ["stamp", "auth", "roadmap"]


def test_peer_claim_refusal_halts(calls, tmp_path):
    calls["claim"] = False
    h = pg.run(_manifest(), PLAN, repo_root=tmp_path)
    assert h.halted_at == contract.HALTS["peer-claim"] and "held by peer" in h.reason
    assert calls["log"][-1] == "claim"


def test_no_subprocess_is_touched(calls, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess used")

    for name in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, boom)
    assert not hasattr(pg, "subprocess")
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None


def _sizing(tmp_path, *, route, tshirt, accepted=None):
    import yaml

    p = tmp_path / "state/sizings/s.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({
            "route": route, "estimate": {"tshirt": tshirt},
            "exit_criterion": {"statement": "done", "accepted": accepted},
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "route,tshirt", [("plan", "L"), ("plan", "M"), ("dispatch", "XS"), ("spec-dispatch", "S")]
)
def test_unasked_sizing_still_in_the_skip_zone_proceeds(calls, tmp_path, route, tshirt):
    _sizing(tmp_path, route=route, tshirt=tshirt)
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None


@pytest.mark.parametrize(
    "route,tshirt",
    [("pm-decision", "XL"), ("plan", "XL"), ("shape", "S"), ("roadmap", "M"), ("goal-setting", "XXL")],
)
def test_resize_out_of_the_skip_zone_halts_at_ready_gate_before_any_other_gate(
    calls, tmp_path, route, tshirt
):
    _sizing(tmp_path, route=route, tshirt=tshirt)
    h = pg.run(_manifest(), PLAN, repo_root=tmp_path)
    assert h.halted_at == "ready-gate" and route in h.reason and tshirt in h.reason
    assert calls["log"] == []


def test_pm_accepted_sizing_is_not_re_halted_by_the_resize_gate(calls, tmp_path):
    _sizing(tmp_path, route="pm-decision", tshirt="XL", accepted={"pm_quote": "go", "on": "d", "mode": "pm"})
    assert pg.run(_manifest(), PLAN, repo_root=tmp_path) is None


def _fired(route, tshirt):
    import dataclasses

    return dataclasses.replace(_manifest(), accepted_route=route, accepted_tshirt=tshirt)


_PM_OK = {"pm_quote": "go", "on": "d", "mode": "pm"}


def test_manifest_without_the_fired_route_and_size_still_loads():
    import json

    old = {k: v for k, v in json.loads(_manifest().to_json()).items() if not k.startswith("accepted_")}
    m = contract.ChainManifest.from_json(json.dumps(old))
    assert (m.accepted_route, m.accepted_tshirt) == (None, None)


def test_accepted_sizing_unchanged_since_the_fire_proceeds(calls, tmp_path):
    _sizing(tmp_path, route="plan", tshirt="M", accepted=_PM_OK)
    assert pg.run(_fired("plan", "M"), PLAN, repo_root=tmp_path) is None


@pytest.mark.parametrize("route,tshirt", [("plan", "XL"), ("pm-decision", "XL"), ("shape", "M"), ("plan", "L")])
def test_resize_after_the_pm_accepted_halts(calls, tmp_path, route, tshirt):
    _sizing(tmp_path, route=route, tshirt=tshirt, accepted=_PM_OK)
    h = pg.run(_fired("plan", "M"), PLAN, repo_root=tmp_path)
    assert h.halted_at == "ready-gate" and "fired as route 'plan' at 'M'" in h.reason
    assert calls["log"] == []


def test_unaccepted_resize_within_the_skip_zone_still_halts_on_the_fired_size(calls, tmp_path):
    _sizing(tmp_path, route="plan", tshirt="L")
    assert pg.run(_fired("plan", "M"), PLAN, repo_root=tmp_path).halted_at == "ready-gate"
