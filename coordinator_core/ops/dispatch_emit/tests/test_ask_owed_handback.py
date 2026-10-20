"""A run with EM-owned rows names them and the exact refire that resumes them."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_REFIRE = ask_compose._refire_js("state/sizings/x.yaml")


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


def test_owed_handback_names_rows_and_the_refire():
    expr = ask_compose._owed_handback_js("state/sizings/x.yaml")
    assert "_manifest.owed.map((o) => o.id + ': ' + (o.detail ?? ''))" in expr
    assert _REFIRE in expr
    assert "refire" in expr
    assert "em-discharge" not in expr
    assert "o.detail" in expr


def test_no_rows_halt_carries_the_owed_handback(monkeypatch):
    script = _script(monkeypatch)
    expr = ask_compose._owed_handback_js("state/sizings/x.yaml")
    fallback = ask_compose._all_em_next_action_js("state/sizings/x.yaml")
    assert f"next_action: {expr} }} : {fallback}" in script


def test_all_em_halt_names_the_discharge_call_per_owed_row():
    expr = ask_compose._all_em_next_action_js("state/sizings/x.yaml")
    assert "_manifest.owed_em_ids" in expr
    assert "owed by the EM" not in expr
    assert "em-discharge" in expr
    assert "_manifest.owed_plan" in expr
    assert _REFIRE in expr
    assert ask_compose._no_manifest_next_action_js("state/sizings/x.yaml") in expr


def test_stage_error_reply_carries_owed_ids_structurally():
    from coordinator_core.ops.dispatch_emit import ask_stage

    exc = ask_stage.AskStageError("x", owed_em_ids=["C3", "C4"], plan_path="p.md")
    assert (exc.owed_em_ids, exc.plan_path) == (["C3", "C4"], "p.md")
    assert ask_stage.AskStageError("y").owed_em_ids is None


def test_return_object_carries_owed_next_action(monkeypatch):
    script = _script(monkeypatch)
    expr = ask_compose._owed_handback_js("state/sizings/x.yaml")
    assert f"owed_next_action: {expr} || null, " in script
