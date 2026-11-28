"""A staged prompt run never splits an inline command from the JS params operand it wraps (example-game-repo-66, cc37e7)."""
from __future__ import annotations

import pytest
import re

from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES, stage_literal_runs
from coordinator_core.ops.workflow_scaffold import _js_string_literal
from coordinator_core.ops.dispatch_emit.tests.test_verifier_prompt_command_grouping import (
    SELECTORS,
    _prompt,
    _record,
    _test_command,
    emitted,
)

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


@pytest.mark.parametrize("prefix,suffix", [
    ("command -- ", " --tail\n"),
    ("Run the scoped test targets: [", "]\n"),
    ("```bash\ncommand -- ", " --tail\n```\n"),
    ("```powershell\n& command -- ", " --tail\n```\n"),
    (CMD, TAIL + "\n"),
])
def test_dynamic_lines_and_fences_stay_on_the_executing_surface(prefix, suffix):
    before = _js_string_literal(HEAD + "\n\n" + prefix)
    after = _js_string_literal(suffix + "\n" + HEAD)
    out, sink = _staged(before + " + operand + " + after)
    assert sink, "unrelated static paragraphs must still stage"
    assert all(prefix not in body and suffix not in body for body in sink.values())
    # Following a pointer reads a separate instruction, never an empty command variant.
    surface = out.replace(_js_string_literal("<<p>>"), "''")
    surface = surface.replace(_js_string_literal("<<p-2>>"), "''")
    assert _js_string_literal(prefix) + " + operand + " in surface
    assert "operand + " + _js_string_literal(suffix) in surface


def test_unknown_escape_keeps_the_entire_run_inline():
    source = "'" + HEAD + r"\u00e9\ncommand -- ' + operand"
    out, sink = _staged(source)
    assert out == source and not sink


def test_default_emitted_read_instructions_execute_the_nonempty_selector_list(emitted):
    root, surfaces = _prompt(emitted)
    instructions = [text for text in surfaces if "Run the scoped test targets: [" in text]
    assert instructions
    for text in instructions:
        match = re.search(r"Run the scoped test targets: \[([^\]]*)\]", text)
        assert match, text
        assert match.group(1).split(", ") == SELECTORS
    command = _test_command(surfaces)
    assert not command.rstrip().endswith("--")
    argv = _record(root, command)
    assert argv[argv.index("--") + 1:] == SELECTORS
