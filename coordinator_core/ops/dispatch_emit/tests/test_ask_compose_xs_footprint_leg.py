"""compose_ask_script: an existing XS sizing with no writes gets one footprint leg ahead of the gate agent."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

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
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


@pytest.fixture
def arm(monkeypatch):
    def _set(value):
        monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: value)

    return _set


def test_xs_with_no_writes_composes_one_footprint_leg_before_the_gate(arm):
    arm("xs")
    script = _compose(writes=(), baton={"path": "state/handoffs/b.md", "deliverable_id": "dlv-1"})
    assert script.count("label: 'footprint'") == 1
    assert script.index("label: 'footprint'") < script.index("label: 'gate'")
    assert "state/handoffs/b.md" in script[script.index("label: 'footprint'") - 2500 : script.index("label: 'footprint'")]
    assert "_writes = (Array.isArray(_fp?.writes)" in script
    assert "if (!_writes.length)" in script


def test_xs_with_writes_composes_no_footprint_leg(arm):
    arm("xs")
    assert "label: 'footprint'" not in _compose(writes=["a.py"])


def test_non_xs_or_unknown_arm_composes_no_footprint_leg(arm):
    for value in ("m_plus", "s", None):
        arm(value)
        assert "label: 'footprint'" not in _compose(writes=())


def test_footprint_prompt_is_a_quoted_string_literal(arm):
    arm("xs")
    script = _compose(writes=(), baton={"path": "state/handoffs/b.md", "deliverable_id": "dlv-1"})
    call = script[script.index("const _fp = await agent(") + len("const _fp = await agent(") :]
    assert call[:2] in ("('", '("', "(`"), call[:80]
