"""Every agent() call emit composes carries exactly one ``sidecar_dir:`` marker."""

from __future__ import annotations

import re

from coordinator_core.ops._workflow_contract import _AGENT_CALL_SITE, _find_matching_paren, scrub
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.agent_sidecar import SIDECAR_DIR_MARKER_PREFIX, SIDECAR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _emit_scripts
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_LABEL_RE = re.compile(r"label: ('[^']*'|\"[^\"]*\")")


def _calls(script: str) -> list[str]:
    scrubbed = scrub(script)
    out = []
    for m in _AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        out.append(script[open_paren : _find_matching_paren(scrubbed, open_paren)])
    return out


def _label(call: str) -> str:
    found = _LABEL_RE.search(call)
    return found.group(1) if found else call[:80]


def _offenders(script: str) -> list[tuple[str, int]]:
    return [
        (_label(call), call.count(SIDECAR_DIR_MARKER_PREFIX))
        for call in _calls(script)
        if call.count(SIDECAR_DIR_MARKER_PREFIX) != 1
    ]


def test_every_ratchet_emit_composition_carries_one_marker_per_agent_call():
    scripts = _emit_scripts(golden=False)
    assert sum(len(_calls(s)) for s in scripts) > 10
    for script in scripts:
        assert _offenders(script) == []


def test_chatty_overseer_survey_summary_and_continue_legs_carry_one_marker():
    chatty = _emit_scripts(golden=False)[5]
    labels = {_label(c) for c in _calls(chatty)}
    assert {"'chatty-overseer'", "'chatty-survey'", "'chatty-summary'"} <= labels
    assert _offenders(chatty) == []


def test_row_prompt_ends_with_its_own_dir_and_names_the_heredoc_fallback():
    row = WaveRow(id="C2", title="t", surface="a.py", writes=["a.py"], reads=[], depends_on=[])
    prompt = emit._row_prompt(row, "docs/plans/2026-10-17-p.md")
    assert prompt.endswith(f"{SIDECAR_DIR_MARKER_PREFIX}{SIDECAR_ROOT}/emit/2026-10-17-p/C2/")
    assert "<<'EOF'" in prompt and "never skip" in prompt


def test_title_only_row_prompt_keys_a_named_constant_run_key():
    row = WaveRow(id="C2", title="t", surface="a.py", writes=["a.py"], reads=[], depends_on=[])
    assert emit._row_prompt(row).endswith(f"{SIDECAR_ROOT}/emit/{emit._SIDECAR_NO_PLAN_KEY}/C2/")


def test_row_footprint_admits_the_sidecar_dir():
    row = WaveRow(id="C2", title="t", surface="a.py", writes=["a.py"], reads=[], depends_on=[])
    contract = emit._row_return_contract(row, "docs/plans/p.md")
    assert f"{SIDECAR_ROOT}/emit/p/C2/" in contract.split("## Files you may write")[0]


def test_the_write_only_clause_carves_out_a_harness_denied_write_and_drops_the_orphan_reason():
    clause = emit._WRITE_TOOL_ONLY_CLAUSE
    assert "never through Bash" in clause and "harness itself denies the Write tool" in clause
    assert "orphan" not in clause
