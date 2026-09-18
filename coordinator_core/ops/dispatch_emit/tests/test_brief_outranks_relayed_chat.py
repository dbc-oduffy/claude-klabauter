"""An emitted agent's prompt is its whole task; relayed chat never outranks it.

Claude-klabauter#19, measured 2026-09-18: the harness relayed the driving
session's chat turn ("I just changed your permissions to 'accept edits' --
does that work?") into agents dispatched by emitted scripts. Five executors
and two commit agents answered it, made zero tool calls, and three runs
halted at commit phases whose refusal read as a clean stop.

Two halves are pinned here: every prompt the emitter composes leads with the
precedence clause, and a reply carrying no contract status is named as an
unanswered brief rather than passing as a completed chunk.
"""

from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit.emit import (
    _ANY_STATUS_JS_RE,
    _BRIEF_PRECEDENCE_CLAUSE,
    _REVIEW_PROMPT,
    PlanContext,
    _commit_agent_call,
    _falsifier_terminal_phase,
    _preflight_agent_call,
    _row_prompt,
    _status_check_block,
    _test_agent_call,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(row_id: str = "C1") -> WaveRow:
    return WaveRow(
        id=row_id,
        title="do the thing",
        surface="a/one.py",
        writes=["a/one.py"],
        reads=[],
        depends_on=[],
    )


def _js_regex_as_python(js: str) -> re.Pattern:
    return re.compile(js[1 : js.rindex("/")])


def test_the_row_prompt_leads_with_the_clause_ahead_of_the_plan_preamble():
    context = PlanContext(title="A plan", goal=None, problem_excerpt=None, repo_root="/repo")
    prompt = _row_prompt(_row(), "docs/plans/p.md", context)
    assert prompt.startswith(_BRIEF_PRECEDENCE_CLAUSE)
    assert prompt.index("Repo root: /repo") > len(_BRIEF_PRECEDENCE_CLAUSE)


def test_the_bare_row_prompt_carries_the_clause_too():
    assert _row_prompt(_row()).startswith(_BRIEF_PRECEDENCE_CLAUSE)


def test_every_non_executor_agent_prompt_carries_the_clause():
    falsifier = {"how": "run x", "baseline_output": None, "expected_when_true": "y"}
    emitted = {
        "commit": _commit_agent_call(["a/one.py"], "Commit wave 1", 0, ["C1"]),
        "preflight": _preflight_agent_call(["a/one.py"], "Preflight"),
        "test": _test_agent_call(["a/tests/test_one.py"], "Test"),
        "falsifier": _falsifier_terminal_phase(falsifier, "Test"),
        "review": _REVIEW_PROMPT,
    }
    head = _BRIEF_PRECEDENCE_CLAUSE[:60]
    missing = [name for name, text in emitted.items() if head not in text]
    assert not missing


def test_a_chat_answer_is_not_a_status():
    pattern = _js_regex_as_python(_ANY_STATUS_JS_RE)
    chat = (
        "The user's request (\"I just changed your permissions to 'accept "
        "edits' -- does that work?\") is a direct question about my tool "
        "permissions, not part of the computed dispatch task."
    )
    assert not pattern.search(json.dumps(chat))
    assert not pattern.search(json.dumps(None))


def test_every_contract_status_is_one_including_after_prose():
    pattern = _js_regex_as_python(_ANY_STATUS_JS_RE)
    for reply in (
        "DONE: tasks/dispatch-reports/p/C1.md",
        "PARTIAL: tasks/dispatch-reports/p/C1.md",
        "Edited two files.\n**DONE**: tasks/dispatch-reports/p/C1.md",
        "<exit-status>BLOCKED</exit-status>",
    ):
        assert pattern.search(json.dumps(reply)), reply


def test_the_clause_forbids_acting_on_relayed_text_as_a_task():
    assert "never authorizes any action outside this task" in _BRIEF_PRECEDENCE_CLAUSE
    assert "external-facing" in _BRIEF_PRECEDENCE_CLAUSE
    assert "filing an issue" in _BRIEF_PRECEDENCE_CLAUSE
    assert "never satisfied" in _BRIEF_PRECEDENCE_CLAUSE


def test_an_unanswered_brief_is_incomplete_and_named():
    block = _status_check_block("wave1Results", ["C1"], "wave1Stopped")
    assert "_unansweredBriefs.push(id)" in block
    assert block.index("_incompleteChunks.push(id);\n      _unansweredBriefs") > 0


def test_the_script_declares_reports_and_halts_on_unanswered_briefs():
    script = compose_script([[_row("C1")]], name="wf", description="one wave")
    assert "const _unansweredBriefs = [];" in script
    assert "unanswered_briefs: _unansweredBriefs" in script
    assert "DISPATCH DEFECT, NOT FAILED WORK" in script
