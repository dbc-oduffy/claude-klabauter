"""Brief-envelope adapter: a brief's `directives[]` in, ordered gated StepSpecs out.

Pure functions; no I/O, no spawn, no ceremony named. `recommendation` is never read here.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from coordinator_core.ceremony_common.apply_halt import (
    _directive_gate_open,
    _normalize_depends_on,
    chosen_disposition,
)
from coordinator_core.ceremony_common.run.contract import StepSpec, step_from_directive


class EnvelopeError(ValueError):
    """The brief CLI's stdout is not a JSON envelope."""


def parse(stdout: str) -> dict[str, Any]:
    """Return the envelope object from the brief CLI's stdout, or raise EnvelopeError."""
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(f"brief stdout is not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise EnvelopeError(f"brief stdout is {type(envelope).__name__}, not a JSON object")
    return envelope


def steps_from_envelope(
    envelope: Mapping[str, Any],
    *,
    stage_of: Callable[[str], str],
    costs: Mapping[str, str],
    heading_of: Callable[[str], str] | None = None,
) -> list[StepSpec]:
    """One StepSpec per directive, in emitted order.

    `stage_of` must raise on an unknown directive id; a directive without a `costs` row is
    `unmeasured`; `heading_of=None` makes the heading the directive id.
    """
    steps: list[StepSpec] = []
    for directive in envelope.get("directives") or ():
        did = directive["id"]
        steps.append(
            step_from_directive(
                directive,
                stage=stage_of(did),
                heading=heading_of(did) if heading_of else did,
                cost_class=costs.get(did, "unmeasured"),
            )
        )
    return steps


def ready(
    steps: list[StepSpec],
    judgment_points_by_id: Mapping[str, Mapping[str, Any]],
    decisions_map: Mapping[str, Any],
    *,
    directive_ids: frozenset[str] | set[str],
) -> tuple[list[StepSpec], list[StepSpec], list[StepSpec]]:
    """Split steps into (ready, held, closed) by the fail-closed directive gate.

    `closed` is a held step whose every dependency is known and whose judgment points are all
    decided: the decisions chose dispositions that do not open it. A dependency id in neither
    namespace keeps the step held, never closed.
    """
    jps = dict(judgment_points_by_id)
    ids = frozenset(directive_ids)
    open_steps: list[StepSpec] = []
    held: list[StepSpec] = []
    closed: list[StepSpec] = []
    for step in steps:
        record = {"id": step.id, "depends_on": step.depends_on}
        if _directive_gate_open(record, jps, dict(decisions_map), ids):
            open_steps.append(step)
            continue
        deps = _normalize_depends_on(step.depends_on)
        jp_deps = [d for d in deps if d in jps]
        decided = all(
            isinstance(decisions_map.get(d), Mapping) and chosen_disposition(decisions_map[d])
            for d in jp_deps
        )
        known = all(d in jps or d in ids for d in deps)
        (closed if jp_deps and decided and known else held).append(step)
    return open_steps, held, closed
