"""compose_ask_script: an XS ask commits its own work; the integration not-run flag is hoisted and set."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.wake_digest import INTEGRATION_NOT_RUN_VAR

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


@pytest.fixture
def xs(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")


def test_flag_is_in_the_unconst_names():
    assert INTEGRATION_NOT_RUN_VAR in ask_compose._REVIEW_RESULT_NAMES


def test_flag_is_declared_at_script_level_false_beside_review_integration(xs):
    script = _compose(writes=["a.py"])
    decl = f"  let {INTEGRATION_NOT_RUN_VAR} = false;"
    assert script.count(decl) == 1
    assert "  let _reviewIntegration = null;" in script
    assert f"const {INTEGRATION_NOT_RUN_VAR} =" not in script


def test_flag_is_set_true_where_xs_empties_the_wave_and_on_the_no_paths_line(xs):
    script = _compose(writes=["a.py"])
    assert f"_xsTail)) {{ {INTEGRATION_NOT_RUN_VAR} = true; }}" in script
    assert f"_gate.arm !== 'xs') {{ {INTEGRATION_NOT_RUN_VAR} = true; }}" in script


def test_digest_inline_review_has_the_bookkeeping_fallback_when_flag_and_empty_wave(xs):
    script = _compose(writes=["a.py"])
    assert f"typeof {INTEGRATION_NOT_RUN_VAR} !== 'undefined' && {INTEGRATION_NOT_RUN_VAR}" in script
    assert "inline_review: (_reviewIntegration ?" in script
    # The owed case still composes a null arm: the fallback is conditional, never unconditional.
    assert ": null)" in script[script.index("inline_review: (_reviewIntegration ?") :][:2000]
