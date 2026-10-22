"""Every workflow.revise next_action params literal the ask script records is valid revise input."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.requirements_ledger import _PARAMS, validate_params
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

_ANCHOR = re.compile(r"op: 'workflow\.revise', params: \{")
_CONTENT_KEYS = {"verbatim", "note", "all_of_pm_ask", "withhold_paths", "credit_chunks", "retract_note", "build_only", "stop_after"}


def _top_level_keys(script: str, start: int) -> list[str]:
    depth, keys, token_start = 1, [], start
    i = start
    while depth:
        ch = script[i]
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "'":
            i = script.index("'", i + 1)
        elif ch == "," and depth == 1:
            token_start = i + 1
        elif ch == ":" and depth == 1:
            keys.append(script[token_start:i].strip())
            token_start = i + 1
        i += 1
    return keys


def _emitted_key_sets() -> list[list[str]]:
    script = _compose(in_run_commit=True, script_path="x.mjs")
    return [_top_level_keys(script, m.end()) for m in _ANCHOR.finditer(script)]


def test_both_emission_sites_are_found():
    assert len(_emitted_key_sets()) == 2


def test_every_recorded_revise_params_key_is_declared_by_revise():
    declared = {f.name for f in _PARAMS}
    for keys in _emitted_key_sets():
        assert set(keys) <= declared, set(keys) - declared


def test_recorded_note_expression_has_a_nonempty_literal_prefix():
    script = _compose(in_run_commit=True, script_path="x.mjs")
    assert script.count("note: 'halted: ' + ") == 2


def test_recorded_revise_params_pass_validation_and_carry_revision_content():
    for keys in _emitted_key_sets():
        params = {k: "halted: x" if k == "note" else "r" for k in keys}
        assert validate_params("workflow.revise", params, _PARAMS) is None
        assert _CONTENT_KEYS & set(keys), "revise refuses params with no revision content"
