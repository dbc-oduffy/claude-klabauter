"""compose_ask_script: planBlitz args carrying chatty put `chatty: true` in the script meta."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_LINE = "  chatty: true,\n"


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_chatty_arg_emits_chatty_meta_line():
    assert _LINE in _compose(plan_blitz_args={"chatty": True, "x": 1})


def test_absent_or_false_chatty_leaves_meta_without_the_line():
    assert _LINE not in _compose()
    assert _LINE not in _compose(plan_blitz_args={"x": 1})
    assert _LINE not in _compose(plan_blitz_args={"chatty": False})


def test_chatty_line_is_the_only_meta_difference():
    on = _compose(plan_blitz_args={"x": 1, "chatty": True})
    off = _compose(plan_blitz_args={"x": 1, "chatty": False})
    on_meta = on.split("export const meta", 1)[1].split("};", 1)[0]
    off_meta = off.split("export const meta", 1)[1].split("};", 1)[0]
    assert on_meta.replace(_LINE, "", 1) == off_meta
