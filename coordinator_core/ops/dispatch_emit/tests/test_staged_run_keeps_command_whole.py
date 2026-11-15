"""A staged prompt run never splits an inline command from the JS params operand it wraps (example-game-repo-66, cc37e7)."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES, stage_literal_runs
from coordinator_core.ops.workflow_scaffold import _js_string_literal

HEAD = "h" * (MIN_STAGE_BYTES + 50)
CMD = "Run `coordinator-invoke dispatch.ask_stage '"
TAIL = "' --repo /r` once and return its JSON reply verbatim."


def _staged(js: str) -> tuple[str, dict[str, str]]:
    sink: dict[str, str] = {}

    def stager(name: str, text: str) -> str:
        sink[name] = text
        return _js_string_literal(f"<<{name}>>")

    return stage_literal_runs(js, "p", stager), sink


def test_the_command_head_before_a_params_operand_stays_inline():
    js = _js_string_literal(HEAD + "\n\n" + CMD) + " + JSON.stringify(x) + " + _js_string_literal(TAIL)
    out, sink = _staged(js)
    assert sink == {"p": HEAD + "\n\n"}
    assert _js_string_literal(CMD) + " + JSON.stringify(x)" in out


def test_the_command_tail_after_a_params_operand_stays_inline():
    rest = "t" * (MIN_STAGE_BYTES + 50)
    js = _js_string_literal("go") + " + JSON.stringify(x) + " + _js_string_literal(TAIL + "\n" + rest)
    out, sink = _staged(js)
    assert sink == {"p": rest}
    assert "JSON.stringify(x) + " + _js_string_literal(TAIL + "\n") + " + " in out


def test_a_balanced_run_is_staged_whole():
    text = HEAD + " `whole cmd` done"
    out, sink = _staged(_js_string_literal(text))
    assert sink == {"p": text} and out == _js_string_literal("<<p>>")
