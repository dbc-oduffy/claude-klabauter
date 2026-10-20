"""Ratchet: every agent() call in every composed workflow carries exactly one ``sidecar_dir:`` marker
and the heredoc-fallback phrase of ``agent_sidecar.sidecar_clause``. No exemption list."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops._workflow_contract import _AGENT_CALL_SITE, _find_matching_paren, scrub
from coordinator_core.ops.dispatch_emit.agent_sidecar import SIDECAR_DIR_MARKER_PREFIX, sidecar_clause
from coordinator_core.ops.dispatch_emit.leg_register import _mask_regex_literals
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _composed
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import (
    _compose as _compose_pipeline,
    _segment,
    _stage,
)

_LABEL_RE = re.compile(r"label: ('[^']*'|\"[^\"]*\")")
_HEREDOC_PHRASES = ("Bash heredoc", "never skip it")
_HOISTED_PROMPT_REF = re.compile(r"\b(_\w*Prompt)\b")


def _calls(script: str) -> list[str]:
    scrubbed = scrub(_mask_regex_literals(script))
    out = []
    for m in _AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        out.append(script[open_paren : _find_matching_paren(scrubbed, open_paren)])
    return out


def _hoisted_definition(script: str, name: str) -> str:
    """Text of the ``const <name> = ...`` line or ``function <name>(...) {...}`` block a call's prompt is hoisted into."""
    found = re.search(rf"^[ \t]*const {name} = [^\n]*", script, re.M) or re.search(
        rf"^[ \t]*function {name}\(.*?\n  \}}", script, re.M | re.S
    )
    return found.group(0) if found else ""


def _expand_hoisted_prompts(call: str, script: str, seen: frozenset[str] = frozenset()) -> str:
    """A call whose prompt is a hoisted ``_xxxPrompt`` is judged by that definition's text, followed transitively.

    Trap: an unresolvable reference stays unexpanded, so the call still reads as bare and offends."""
    out = call
    for name in dict.fromkeys(_HOISTED_PROMPT_REF.findall(call)):
        if name in seen:
            continue
        definition = _hoisted_definition(script, name)
        out += _expand_hoisted_prompts(definition, script, seen | {name}) if definition else ""
    return out


def _label(call: str) -> str:
    found = _LABEL_RE.search(call)
    return found.group(1) if found else call[:80]


def _offences(workflow: str, script: str) -> list[str]:
    bad = []
    for raw in _calls(script):
        call = _expand_hoisted_prompts(raw, script)
        count = call.count(SIDECAR_DIR_MARKER_PREFIX)
        if count != 1:
            bad.append(f"{workflow} {_label(call)}: {count} sidecar_dir markers")
        missing = [p for p in _HEREDOC_PHRASES if p not in call]
        if missing:
            bad.append(f"{workflow} {_label(call)}: heredoc fallback missing {missing}")
    return bad


def test_the_phrases_checked_are_the_ones_the_clause_carries():
    clause = sidecar_clause("x/")
    assert all(p in clause for p in _HEREDOC_PHRASES)


def test_every_composed_workflow_agent_call_names_its_sidecar_and_the_heredoc_fallback(tmp_path):
    composed = dict(_composed(tmp_path))
    composed["pipeline"] = [
        _compose_pipeline(
            [
                _segment(tmp_path, "pa", [_stage("make"), _stage("use", depends_on=["make"])]),
                _segment(tmp_path, "pb", [_stage("work")]),
            ]
        )
    ]
    offences = []
    total = 0
    for workflow, scripts in composed.items():
        for script in scripts:
            total += len(_calls(script))
            offences += _offences(workflow, script)
    assert total > 50
    assert not offences, "\n".join(sorted(set(offences)))


def test_the_ratchet_refuses_a_call_without_the_clause():
    script = "const r = await agent('do a thing', { label: 'bare', agentType: 'x', model: 'sonnet' });"
    assert _offences("t", script) == [
        "t 'bare': 0 sidecar_dir markers",
        "t 'bare': heredoc fallback missing ['Bash heredoc', 'never skip it']",
    ]


def test_the_ratchet_refuses_a_doubled_marker():
    clause = sidecar_clause("d/")
    script = f"await agent({'x' + clause + clause!r}, {{ label: 'dup' }});"
    assert any("2 sidecar_dir markers" in o for o in _offences("t", script))


@pytest.mark.parametrize("phrase", _HEREDOC_PHRASES)
def test_the_ratchet_refuses_a_call_missing_one_phrase(phrase):
    script = f"await agent({sidecar_clause('d/').replace(phrase, 'zzz')!r}, {{ label: 'p' }});"
    assert any("heredoc fallback missing" in o for o in _offences("t", script))


def test_a_hoisted_prompt_is_judged_by_its_definition_transitively():
    clause = sidecar_clause("d/")
    call = "await agent(_groupPrompt(g), { label: 'work:' + g });"
    group = "function _groupPrompt(g) {\n    return ms.map((m) => _rowPrompt(m)).join('');\n  }\n"
    bare = f"const _rowPrompt = (r) => 'x';\n{group}{call}"
    carried = f"const _rowPrompt = (r) => {'x' + clause!r};\n{group}{call}"
    assert _offences("t", bare)
    assert _offences("t", carried) == []
    assert _offences("t", f"{group}{call}")
