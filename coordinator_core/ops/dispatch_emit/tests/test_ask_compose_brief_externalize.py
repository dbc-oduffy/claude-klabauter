"""Every leg's large static brief body leaves the composed ask script for a staged file; no stager leaves it inline."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_RUN = "run-1"
_WIDENED = ("verdict", "row-prompt", "plan", "overlay-verify", "remnant-complete", "chain-emit")


def _compose(**over) -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=_RUN, session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW, **over,
    )


def test_legs_beyond_the_original_set_stage_their_bodies():
    sink: dict[str, str] = {}
    script = _compose(staged_out=sink)
    for name in _WIDENED:
        assert name in sink, name
        assert f"REPO/{RECORD_DIR_ROOT}/{_RUN}/prompts/{name}.md in full" in script, name


def test_staging_every_leg_shrinks_the_script_and_the_text_round_trips(monkeypatch):
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", False)
    sink: dict[str, str] = {}
    staged = _compose(staged_out=sink)
    inline = _compose()
    assert len(staged.encode()) < len(inline.encode())
    for name, text in sink.items():
        pointer = _js_string_literal(
            f"Read REPO/{RECORD_DIR_ROOT}/{_RUN}/prompts/{name}.md in full and follow it as the rest of this brief.\n"
        )
        assert pointer in staged, name
        staged = staged.replace(pointer, _js_string_literal(text))
    assert prompt_fragments.fold_literals(staged) == prompt_fragments.fold_literals(inline)


def test_a_name_held_by_other_text_files_the_body_under_a_numbered_name():
    sink: dict[str, str] = {}
    stager = ask_compose._unique_stager(lambda name, text: sink.setdefault(name, text) and name, sink)
    assert stager("leg", "one") == "leg"
    assert stager("leg", "one") == "leg"
    assert stager("leg", "two") == "leg~2"
    assert sink == {"leg": "one", "leg~2": "two"}


def test_a_compose_after_a_staged_one_stages_nothing():
    _compose(staged_out={})
    assert ask_compose._ACTIVE_STAGER.get() is None
    assert "/prompts/" not in _compose()


def test_a_label_names_a_safe_file():
    big = ("A large static sentence the agent must follow. " * 20).replace("'", "")
    expr = _js_string_literal(big)
    sink: dict[str, str] = {}
    out = ask_compose._stage_expr(expr, "review:coordinator:code-reviewer", lambda n, t: sink.setdefault(n, t) and n)
    assert out == "review-coordinator-code-reviewer"
    assert sink == {"review-coordinator-code-reviewer": big}


@pytest.mark.parametrize("name", _WIDENED)
def test_a_staged_body_is_at_least_the_staging_floor(name):
    from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES

    sink: dict[str, str] = {}
    _compose(staged_out=sink)
    assert len(sink[name].encode()) >= MIN_STAGE_BYTES
