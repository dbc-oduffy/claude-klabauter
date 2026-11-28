"""The staged plan-prep-repair prompt never ends mid-sentence at its plan-path operand."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES, stage_literal_runs
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_STEM = "The plan prep gate refused the plan at "


def _compose_staged() -> tuple[str, dict[str, str]]:
    sink: dict[str, str] = {}
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", staged_out=sink, in_run_commit=True, script_path="scratch/warp/a.workflow.mjs",
        **REVIEW_KW,
    )
    return script, sink


def test_no_staged_file_ends_at_the_repair_prompts_operand_split():
    script, sink = _compose_staged()
    assert sink, "compose staged nothing; the check would pass vacuously"
    assert not [n for n, t in sink.items() if t.rstrip(" ").endswith(_STEM.rstrip())], "a staged file ends at the operand"
    repair = next(ln for ln in script.splitlines() if "label: 'plan-prep-repair'" in ln)
    # The sentence is whole inline, the operand beside it, nothing of it lost to a file.
    assert re.search(re.escape(_STEM) + r"[^\n]*' \+ _planRel \+ ' with verdict ", repair)


def test_a_balanced_run_ending_mid_line_before_an_operand_keeps_that_line_inline():
    head = "h" * (MIN_STAGE_BYTES + 50) + "\n\n"
    js = _js_string_literal(head + "Refused at ") + " + _planRel + " + _js_string_literal(" with verdict")
    sink: dict[str, str] = {}
    out = stage_literal_runs(js, "p", lambda n, t: sink.setdefault(n, t) and _js_string_literal(f"<<{n}>>"))
    assert sink == {"p": head}
    assert _js_string_literal("Refused at ") + " + _planRel" in out


def test_a_run_ending_on_a_newline_before_an_operand_is_staged_whole():
    head = "h" * (MIN_STAGE_BYTES + 50) + "\n"
    js = _js_string_literal(head) + " + _x"
    sink: dict[str, str] = {}
    stage_literal_runs(js, "p", lambda n, t: sink.setdefault(n, t) and _js_string_literal(f"<<{n}>>"))
    assert sink == {"p": head}
