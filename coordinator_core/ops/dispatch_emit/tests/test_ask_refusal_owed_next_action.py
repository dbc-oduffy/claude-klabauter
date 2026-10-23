"""compose_ask_script: every halt returns a non-null owed_next_action; a pull's rulings fall back to its blocking findings."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_return_object_falls_back_to_the_halt_text():
    script = _script()
    owed = ask_compose._owed_handback_js(None)
    assert f"owed_next_action: {owed} || _haltNextActionText() || null, " in script


def test_halt_text_is_read_by_the_return_object_right_after_the_refire_finalises():
    script = _script()
    tail = script[script.index("  _refireNextAction();\n  return {"):]
    assert "function _haltNextActionText()" in script
    assert "_haltNextActionText() || null" in tail


def test_helper_renders_each_next_action_shape():
    helper = ask_compose._halt_next_action_text_helper_js("state/sizings/x.yaml")
    assert "if (!_halted || typeof _halted !== 'object') return '';" in helper
    assert "if (typeof _na === 'string') return _na;" in helper
    assert "'kind: '" in helper and "_na.input.revise.call" in helper and "'refire: '" in helper
    assert f"_halted.halted === {ask_compose._lit('refusal')}) return 'Refire: ' + " + ask_compose._refire_js("state/sizings/x.yaml") in helper


def test_pull_rulings_fall_back_to_blocking_findings():
    script = _script()
    assert "_revised.rulings.length ? _revised.rulings" in script
    assert "_pulledEntry.blocking" in script
    assert "source: 'blocking'" in script
    assert "subject: String(f).slice(0, 300), ask: String(f)" in script
