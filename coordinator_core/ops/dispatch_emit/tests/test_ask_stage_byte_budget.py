"""Per-stage byte budgets for the composed ask script (bare and m_plus arms).

Fixtures follow test_ask_script_byte_headroom: hoisting on, plugin root resolved at import, the
m_plus arm composed over a blitz padded to 279 KB.
"""

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage_budget as asb, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import (
    _PLUGIN_ROOT,
    _padded_plan_blitz_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _compose(monkeypatch, *, arm, plan_blitz_text=None) -> str:
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    assert _PLUGIN_ROOT, "plugin root did not resolve at import"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    if arm:
        monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    kw = {"plan_blitz_text": plan_blitz_text} if plan_blitz_text is not None else {}
    return ask_compose.compose_ask_script(
        repo_root=str(_REPO_ROOT), prompt="probe ask", sizing_rel=None,
        run_id="ask-probe-000000", session_id="probe", **kw,
    )


def _measure(monkeypatch, arm_key):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    if arm_key == "m_plus":
        text = _padded_plan_blitz_text()
        script = _compose(monkeypatch, arm="m_plus", plan_blitz_text=text)
        wrapped = asb.stage_texts(plan_blitz_text=text)
    else:
        script = _compose(monkeypatch, arm=None)
        wrapped = asb.stage_texts()
    measured = asb.stage_bytes(script, wrapped)
    print(f"{arm_key} stage bytes: {measured}; roadmap carried: {'async function roadmapBlitz(' in script}")
    assert sum(measured.values()) == len(script.encode("utf-8"))
    return measured


def test_bare_ask_stages_stay_inside_their_budgets(monkeypatch):
    measured = _measure(monkeypatch, "bare")
    assert not asb.budget_failures("bare", measured), asb.budget_failures("bare", measured)


def test_m_plus_ask_stages_stay_inside_their_budgets(monkeypatch):
    measured = _measure(monkeypatch, "m_plus")
    assert measured["planBlitz"] > 0
    assert not asb.budget_failures("m_plus", measured), asb.budget_failures("m_plus", measured)


def test_a_stage_over_budget_fails_and_a_slack_stage_names_the_new_number(monkeypatch):
    budget = asb.STAGE_BUDGETS["bare"]["core"]
    over = {"core": budget + 1}
    assert any("core" in f and "budget" in f for f in asb.budget_failures("bare", over))
    under = {"core": budget - 2 * asb.RATCHET_SLACK - 1}
    fails = asb.budget_failures("bare", under)
    assert any(str(under["core"] + asb.RATCHET_SLACK) in f for f in fails)
    within = {s: asb.STAGE_BUDGETS["bare"][s] for s in asb.STAGES}
    assert asb.budget_failures("bare", within) == []


def test_stage_bytes_attributes_a_planted_block():
    block = "async function planBlitz(args) {\nconst x = 1\n}\n"
    script = "head\n" + block + "tail\n"
    got = asb.stage_bytes(script, {"planBlitz": block})
    assert got["planBlitz"] == len(block.encode())
    assert got["core"] == len(script.encode()) - len(block.encode())
    assert got["staged"] == 0 and got["roadmapBlitz"] == 0
    assert asb.stage_bytes("head\n", {"planBlitz": block})["planBlitz"] == 0
