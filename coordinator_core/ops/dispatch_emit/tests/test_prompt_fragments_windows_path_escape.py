"""A Windows path in run text leaves every `_PF` chunk a valid single-line JS literal."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import prompt_fragments as pf
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import _decode
from coordinator_core.ops.workflow_scaffold import _js_string_literal


@pytest.fixture(autouse=True)
def _hoisting_on(monkeypatch):
    monkeypatch.setattr(pf, "HOIST_ENABLED", True)


BS = chr(92)
ANCHOR = "// anchor"
TAIL = " The rest of this sentence pads the chunk past the hoist minimum. "
_TERMINATORS = re.compile("[\r\n  ]")


def _script(literal: str, uses: int = 4) -> str:
    return ANCHOR + "\n" + "\n".join(f"agent({literal} + x{i} + {literal});" for i in range(uses)) + "\n"


def _assert_every_literal_is_one_line(script: str) -> None:
    for s, e in pf._scan_literals(script):
        assert not _TERMINATORS.search(script[s:e]), script[s:e]


def test_properly_escaped_windows_path_hoists_unchanged() -> None:
    path = "E:" + BS + "dev" + BS + "research" + BS + "example-game-repo"
    lit = _js_string_literal("See " + path + "." + TAIL + "Second sentence." + TAIL)
    out = pf.hoist_repeated_fragments(_script(lit), anchor=ANCHOR)
    assert "_PF0" in out
    _assert_every_literal_is_one_line(out)
    assert pf.resolve_fragment_refs(out) == pf.resolve_fragment_refs(_script(lit))
    assert BS * 2 + "research" in out


def test_raw_carriage_return_after_a_path_is_escaped_not_emitted_raw() -> None:
    decoded = _decode("E:" + BS * 2 + "dev" + BS + "research and more." + TAIL)
    assert decoded is not None and "\r" in decoded
    lit = _js_string_literal(decoded)
    assert "\r" in lit
    out = pf.hoist_repeated_fragments(_script(lit), anchor=ANCHOR)
    _assert_every_literal_is_one_line(out)
    assert BS + "r" in out


def test_raw_terminators_are_escaped_even_when_nothing_hoists() -> None:
    lit = "'a" + "\r" + "b" + "\n" + "c" + " " + "d'"
    out = pf.hoist_repeated_fragments(ANCHOR + "\nagent(" + lit + ");\n", anchor=ANCHOR)
    assert "_PF" not in out
    _assert_every_literal_is_one_line(out)
    assert out.endswith("agent('a" + BS + "rb" + BS + "nc" + BS + "u2028d');\n")


def test_escape_that_spells_cr_is_left_as_written() -> None:
    lit = "'E:" + BS * 2 + "dev" + BS + "research" + TAIL + "'"
    out = pf.hoist_repeated_fragments(_script(lit), anchor=ANCHOR)
    assert BS + "research" in pf.resolve_fragment_refs(out)
    _assert_every_literal_is_one_line(out)
