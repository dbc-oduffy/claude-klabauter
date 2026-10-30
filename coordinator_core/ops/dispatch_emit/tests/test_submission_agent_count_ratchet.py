"""Ratchet: a multi-ask submission spends a fixed number of agents however many children it starts, and
no per-child leg runs a deterministic coordinator-invoke op."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pytest

from coordinator_core.ops.dispatch_emit import fan_launcher as fl
from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.tests import test_leg_register_ratchet as legs

#: Per-child legs allowed to run an op, by label prefix: a chain hop fires only on an M+ hand-off after
#: that child's own run. Shrink-only.
_PER_CHILD_CONDITIONAL: frozenset[str] = frozenset({"chain-emit-"})
_PER_CHILD_CONDITIONAL_MAX = 1

#: Legs the launcher runs once per submission, whatever the child count. Closed: a new one is a review.
_PER_SUBMISSION_AGENTS: frozenset[str] = frozenset({"emit-batch", "abandon-batch"})
_HAPPY_PATH_EMIT_AGENTS = 1
_PER_CHILD_ROOT = "_runChild"
_HAPPY_PATH_ROOT = "_startAll"

_FUNCTION = re.compile(r"(?:\basync\s+)?\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
_INVOKE_IDENT = re.compile(r"\bINVOKE\b")


@dataclass(frozen=True)
class Site:
    label: str
    phase: Optional[str]
    agent_type: Optional[str]
    function: Optional[str]
    relay_shaped: bool
    label_is_literal: bool


@dataclass(frozen=True)
class Analysis:
    sites: tuple[Site, ...]
    reach: dict[str, frozenset[str]]  # function -> every function it transitively calls, itself included
    callers: dict[str, list[str]]  # function -> literal label prefixes its call sites pass last

    def per_child(self) -> list[Site]:
        reached = self.reach[_PER_CHILD_ROOT]
        return [s for s in self.sites if s.function in reached]

    def once(self) -> list[Site]:
        reached = self.reach[_PER_CHILD_ROOT]
        return [s for s in self.sites if s.function not in reached]


def _functions(scrubbed: str) -> dict[str, tuple[int, int]]:
    spans: dict[str, tuple[int, int]] = {}
    for m in _FUNCTION.finditer(scrubbed):
        close_paren = lr._find_matching_paren(scrubbed, m.end() - 1)
        start = scrubbed.index("{", close_paren)
        depth, i = 0, start
        while i < len(scrubbed):
            depth += scrubbed[i] == "{"
            depth -= scrubbed[i] == "}"
            i += 1
            if depth == 0:
                break
        spans[m.group(1)] = (start, i)
    return spans


def _enclosing(spans: dict[str, tuple[int, int]], pos: int) -> Optional[str]:
    inside = [(start, name) for name, (start, end) in spans.items() if start <= pos < end]
    return max(inside)[1] if inside else None


def _last_arg(scrubbed: str, raw: str, open_paren: int) -> str:
    close = lr._find_matching_paren(scrubbed, open_paren)
    depth, cut = 0, open_paren + 1
    for i in range(open_paren, close):
        ch = scrubbed[i]
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 1:
            cut = i + 1
    return raw[cut : close - 1].strip()


def _relay_shaped(scrubbed_args: str, raw_args: str) -> bool:
    return bool(_INVOKE_IDENT.search(scrubbed_args) or "coordinator-invoke" in raw_args or lr.relay_markers(raw_args))


def analyse(script: str) -> Analysis:
    scrubbed = lr.scrub(lr._mask_regex_literals(script))
    spans = _functions(scrubbed)
    sites: list[Site] = []
    for m in re.finditer(r"\bagent\s*\(", scrubbed):
        open_paren = m.end() - 1
        close = lr._find_matching_paren(scrubbed, open_paren)
        s_args, r_args = scrubbed[open_paren:close], script[open_paren:close]
        value = lr._option_value(s_args, r_args, "label")
        sites.append(
            Site(
                label=lr._label(s_args, r_args),
                phase=lr._string_option(s_args, r_args, "phase"),
                agent_type=lr._string_option(s_args, r_args, "agentType"),
                function=_enclosing(spans, open_paren),
                relay_shaped=_relay_shaped(s_args, r_args),
                label_is_literal=value is not None and lr._literal_prefix(value) is not None,
            )
        )
    calls = {
        name: {callee for callee in spans if callee != name and re.search(rf"\b{re.escape(callee)}\s*\(", scrubbed[a:b])}
        for name, (a, b) in spans.items()
    }
    reach: dict[str, frozenset[str]] = {}
    for name in spans:
        seen, todo = {name}, [name]
        while todo:
            for callee in calls[todo.pop()]:
                if callee not in seen:
                    seen.add(callee)
                    todo.append(callee)
        reach[name] = frozenset(seen)
    callers: dict[str, list[str]] = {name: [] for name in spans}
    for name, (a, b) in spans.items():
        for callee in spans:
            if callee == name:
                continue
            for call in re.finditer(rf"\b{re.escape(callee)}\s*\(", scrubbed[a:b]):
                prefix = lr._literal_prefix(_last_arg(scrubbed, script, a + call.end() - 1))
                if prefix:
                    callers[callee].append(prefix)
    return Analysis(tuple(sites), reach, callers)


def _site_names(analysis: Analysis, site: Site) -> list[str]:
    """Label prefixes a site runs under: its own literal, or those its enclosing helper's callers pass."""
    if site.label_is_literal:
        return [site.label]
    return analysis.callers.get(site.function or "", []) or [site.label]


def per_child_op_refusals(
    analysis: Analysis, conditional: Iterable[str] = _PER_CHILD_CONDITIONAL
) -> list[str]:
    """One message per per-child agent() site that runs an op and is not named conditional."""
    refusals = []
    for site in analysis.per_child():
        if not site.relay_shaped:
            continue
        names = _site_names(analysis, site)
        unnamed = [n for n in names if not any(n.startswith(c) for c in conditional)]
        if unnamed:
            refusals.append(
                f"per-child agent() {unnamed} in {site.function}() runs a deterministic op: "
                "batch it into the once-per-submission emit-batch leg"
            )
    return refusals


def _launcher(tmp_path: Path, n: int) -> str:
    path = fl.write_launcher(tmp_path, f"ask-parent-{n}", _children(n), None)
    return path.read_text(encoding="utf-8")


def _children(n: int) -> list:
    return [
        fl.LaunchChild(
            run_id=f"ask-r{i}",
            sizing_rel=f"state/sizings/ask-r{i}.yaml",
            emit_params={"ask": True, "sizing_path": f"state/sizings/ask-r{i}.yaml", "reserved_run_id": f"ask-r{i}"},
        )
        for i in range(n)
    ]


def _happy_path_emit_agents(analysis: Analysis) -> list[Site]:
    reached = analysis.reach[_HAPPY_PATH_ROOT] - analysis.reach[_PER_CHILD_ROOT]
    return [s for s in analysis.sites if s.function in reached and s.phase == "emit"]


@pytest.mark.parametrize("n", [1, 2, 24])
def test_the_happy_path_spends_one_emit_agent_however_many_children(tmp_path, n):
    analysis = analyse(_launcher(tmp_path, n))
    emit = _happy_path_emit_agents(analysis)
    assert len(emit) == _HAPPY_PATH_EMIT_AGENTS, [s.label for s in emit]
    assert {s.agent_type for s in analysis.sites} == {legs._OP_RELAY_TYPE}


def test_the_agent_sites_do_not_depend_on_the_child_count(tmp_path):
    shapes = {n: sorted((s.label, s.agent_type, s.function) for s in analyse(_launcher(tmp_path, n)).sites) for n in (1, 2, 24)}
    assert shapes[1] == shapes[2] == shapes[24]


def test_the_once_per_submission_legs_are_the_closed_set(tmp_path):
    analysis = analyse(_launcher(tmp_path, 3))
    assert {s.label for s in analysis.once()} == set(_PER_SUBMISSION_AGENTS)
    assert not any(s.relay_shaped is False for s in analysis.once())


def test_no_per_child_leg_runs_an_op_outside_the_conditional_set(tmp_path):
    analysis = analyse(_launcher(tmp_path, 2))
    assert per_child_op_refusals(analysis) == []
    named = {n for s in analysis.per_child() for n in _site_names(analysis, s)}
    assert named == set(_PER_CHILD_CONDITIONAL), "the conditional set names a leg the launcher no longer composes"


def test_the_conditional_set_only_shrinks():
    assert _PER_CHILD_CONDITIONAL <= frozenset({"chain-emit-"})
    assert len(_PER_CHILD_CONDITIONAL) <= _PER_CHILD_CONDITIONAL_MAX


def test_the_refusal_fires_when_the_conditional_name_is_dropped(tmp_path):
    analysis = analyse(_launcher(tmp_path, 2))
    refusals = per_child_op_refusals(analysis, conditional=())
    assert refusals and "chain-emit-" in refusals[0] and "_emitLeg" in refusals[0]


_PLANT = (
    "async function _runChild(child, emitted) {\n"
    "  await agent('Run `' + INVOKE + ' dispatch.emit` once and return its JSON reply verbatim', "
    "{ label: 'emit:' + child.run_id, phase: 'emit', agentType: 'general-purpose', model: 'haiku' });\n"
)


def test_a_planted_per_child_op_leg_fails_by_name(tmp_path):
    script = _launcher(tmp_path, 2)
    head = "async function _runChild(child, emitted) {\n"
    assert head in script
    planted = analyse(script.replace(head, _PLANT, 1))
    refusals = per_child_op_refusals(planted)
    assert len(refusals) == 1 and "emit:" in refusals[0] and "_runChild" in refusals[0], refusals
    assert len(_happy_path_emit_agents(planted)) == _HAPPY_PATH_EMIT_AGENTS, "the plant is per child, not per submission"


def test_a_planted_leg_in_a_callee_of_the_child_runner_fails_by_name(tmp_path):
    script = _launcher(tmp_path, 2)
    helper = (
        "async function _sneak(child) {\n"
        "  return agent('Run exactly `git rev-parse HEAD` and relay it', { label: 'sneak', phase: 'x' });\n}\n"
    )
    script = script.replace("async function _runChild(", helper + "async function _runChild(", 1)
    script = script.replace("const refusal = _emitRefusal(child, emitted, false);", "await _sneak(child);\n  const refusal = _emitRefusal(child, emitted, false);", 1)
    refusals = per_child_op_refusals(analyse(script))
    assert any("sneak" in r and "_sneak" in r for r in refusals), refusals


def test_a_per_submission_leg_running_an_op_is_not_refused(tmp_path):
    analysis = analyse(_launcher(tmp_path, 2))
    assert any(s.relay_shaped for s in analysis.once())
    assert all(s.function not in analysis.reach[_PER_CHILD_ROOT] for s in analysis.once())


def test_a_second_happy_path_emit_agent_breaks_the_count(tmp_path):
    script = _launcher(tmp_path, 2)
    extra = "  await agent('Run `' + INVOKE + ' x`', { label: 'emit-more', phase: 'emit', agentType: 'coordinator:op-relay' });\n"
    marker = "async function _startAll() {\n"
    planted = analyse(script.replace(marker, marker + extra, 1))
    assert len(_happy_path_emit_agents(planted)) == _HAPPY_PATH_EMIT_AGENTS + 1


#: Composed emit scripts: a label family fans one leg per row, wave or reviewer, so a relay-shaped prompt
#: under one is a per-item op leg unless the register declares the family EARNS_ITS_COST (it also judges).
def _family_relay_offenders(composed: dict[str, list[str]]) -> list[str]:
    found = set()
    for workflow, scripts in composed.items():
        for script in scripts:
            scrubbed = lr.scrub(lr._mask_regex_literals(script))
            for m in re.finditer(r"\bagent\s*\(", scrubbed):
                open_paren = m.end() - 1
                close = lr._find_matching_paren(scrubbed, open_paren)
                s_args, r_args = scrubbed[open_paren:close], script[open_paren:close]
                label = lr._label(s_args, r_args)
                family = next((p for p in legs._FAMILY_PREFIXES if label.startswith(p)), None)
                decl = lr.lookup(workflow, family) if family else None
                judges = decl is not None and decl.cls is lr.LegClass.EARNS_ITS_COST
                if family and _relay_shaped(s_args, r_args) and not judges:
                    found.add(f"({workflow}, {family}) runs a relay-shaped prompt per item")
    return sorted(found)


def test_no_label_family_in_a_composed_script_runs_a_relay_prompt(tmp_path):
    assert _family_relay_offenders(legs._composed(tmp_path)) == []


def test_the_family_sweep_fires_on_a_planted_family_relay_leg():
    script = (
        "for (const r of rows) await agent('Run `' + INVOKE + ' dispatch.emit` and return its JSON reply verbatim', "
        "{ label: 'work:' + r.id, phase: 'p', agentType: 'general-purpose', model: 'haiku' });"
    )
    assert _family_relay_offenders({"stray": [script]}) == ["(stray, work:) runs a relay-shaped prompt per item"]
    assert _family_relay_offenders({"emit": [script]}) == [], "a declared EARNS_ITS_COST family also judges"
    assert _family_relay_offenders({"stray": ["await agent('judge it', { label: 'work:' + r.id, phase: 'p' });"]}) == []
