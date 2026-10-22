"""The ledger-remaining prompt names only the leg's footprint, and the ledger-check halt names the claim param."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import CLAIM_PARAM
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def test_remaining_prompt_has_no_manifest_union_fallback(monkeypatch):
    script = _script(monkeypatch)
    assert "flatMap((r) => r.writes" not in script
    assert "remaining_leg?.writes ?? []" in script or "?.writes ?? []).join(', ')" in script


def test_halt_next_action_names_the_claim_param(monkeypatch):
    script = _script(monkeypatch)
    start = script.index("halted: 'ledger-check'")
    text = script[start : script.index("}; }", start)]
    assert f"{CLAIM_PARAM}: true" in text
    assert "not in the stage manifest rulable_ids" in text
    assert "workflow.revise" in text
