"""The composed ask script collects settled consumed replies and hands them to the halt-record commit."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, row_returns
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
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
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_the_binding_is_declared_before_execute_and_filled_only_at_the_settled_replies_site(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("let _consumedReturns = {};") == 1
    decl = script.index("let _consumedReturns = {};")
    assert decl < script.index("const _rows = {};")
    assert script.index("let _deliveryNotes = '';") < decl
    fills = [i for i in range(len(script)) if script.startswith("_consumedReturns[", i)]
    assert len(fills) == 1
    settled = script.index("await Promise.all(Object.values(_rows));")
    assert settled < fills[0] < script.index("await Promise.all(_verifications);")
    site = script[settled : script.index("await Promise.all(_verifications);")]
    assert "_DEP_NON_DONE_RE.test(" in site and "_DEP_ANY_STATUS_RE.test(" in site
    assert "agent(" not in site


def test_the_halt_params_block_names_the_key_even_with_no_deps(monkeypatch):
    script = _script(monkeypatch)
    fn = script[script.index("async function _commitHaltRecord()") :]
    fn = fn[: fn.index("\n  }") if "\n  }" in fn else len(fn)]
    assert "_terminalCommitParamsBlock({" in fn
    assert f"{row_returns.CONSUMED_RETURNS_PARAM}: _consumedReturns" in fn
