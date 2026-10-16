"""Audit: every commit leg any composed claude-klabauter workflow emits runs on haiku."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.tests.test_leg_register_ratchet import _composed

_COMMIT_AGENT = "coordinator:git-commit-agent"
_REQUIRED = {
    ("ask", "commit"),
    ("ask", "halt-commit"),
    ("emit", "commit:wave-"),
    ("grind", "commit"),
    ("grind", "review-fix-commit"),
}


def _is_commit_leg(leg: lr.EmittedLeg) -> bool:
    return leg.agent_type == _COMMIT_AGENT or "commit" in leg.label


def _offenders(composed: dict[str, list[str]]) -> list[tuple[str, str, str]]:
    return [
        (workflow, leg.label, str(leg.model))
        for workflow, scripts in composed.items()
        for script in scripts
        for leg in lr.emitted_legs(script)
        if _is_commit_leg(leg) and leg.model != "haiku"
    ]


def _population(composed: dict[str, list[str]]) -> set[tuple[str, str]]:
    found = set()
    for workflow, scripts in composed.items():
        for script in scripts:
            for leg in lr.emitted_legs(script):
                if _is_commit_leg(leg):
                    found.add((workflow, leg.label))
    return found


def _swap_model(script: str, label: str) -> str:
    scrubbed = lr.scrub(lr._mask_regex_literals(script))
    for m in lr._AGENT_CALL_SITE.finditer(scrubbed):
        open_paren = m.end() - 1
        close = lr._find_matching_paren(scrubbed, open_paren)
        if lr._label(scrubbed[open_paren:close], script[open_paren:close]) == label:
            call = script[open_paren:close]
            assert "'haiku'" in call
            return script[:open_paren] + call.replace("'haiku'", "'sonnet'", 1) + script[close:]
    raise AssertionError(f"no leg labelled {label}")


def test_every_commit_leg_emits_haiku(tmp_path: Path):
    composed = _composed(tmp_path)
    assert _offenders(composed) == []


def test_the_commit_population_is_not_vacuous(tmp_path: Path):
    population = _population(_composed(tmp_path))
    assert _REQUIRED <= population, sorted(_REQUIRED - population)


def test_a_sonnet_commit_leg_is_named(tmp_path: Path):
    composed = _composed(tmp_path)
    scripts = composed["ask"]
    idx = next(i for i, s in enumerate(scripts) if any(l.label == "halt-commit" for l in lr.emitted_legs(s)))
    mutated = {**composed, "ask": [*scripts[:idx], _swap_model(scripts[idx], "halt-commit"), *scripts[idx + 1 :]]}
    assert ("ask", "halt-commit", "sonnet") in _offenders(mutated)
