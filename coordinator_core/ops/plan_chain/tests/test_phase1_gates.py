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
