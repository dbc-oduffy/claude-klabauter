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
    assert f"next_action: {expr} }} : {{}})" in script


def test_return_object_carries_owed_next_action(monkeypatch):
    script = _script(monkeypatch)
    expr = ask_compose._owed_handback_js("state/sizings/x.yaml")
    assert f"owed_next_action: {expr} || null, " in script
