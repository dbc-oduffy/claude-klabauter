"""coordinator_core.ceremony_common.apply_halt — the shared halt contract
every ceremony-close computed-skill assembler's `apply()` half runs its
directive loop against: the value-aware judgment-point gate predicate, the
unrecognized-directive signal, and the ceremony family's exit-code ladder.

Extraction context: `workday_complete/apply.py` and `workweek_complete/apply.py`
carried byte-identical copies of `_directive_gate_open` /
`_disposition_resolves_directive` (workday: lines 283-306 pre-extraction;
workweek: lines 248-271 pre-extraction) plus an identical four-member
exit-code ladder (`HALTED_AT_JUDGMENT=1`, `DIRECTIVE_FAILED=2`,
`TRANSPORT_FAIL=3`, `PARTIAL_MUTATION=4`) built via `extend_exit_codes`.
This module factors that shared shape out of `ceremony_common` — the
family-scoped substrate `workday_complete`/`workweek_complete` already
share `build_ceremony_close_tail` (`ceremony_common.tail`) through — before
a third hand-copy (`workstream_complete/apply.py`) could land and turn a
two-file import substitution into a three-way reconciliation.

This is `ceremony_common`'s own factoring surface (created by B1's chunk
C5) being extended per the workstream-complete-computed-frontage plan's
D-1 third-surface note. It is explicitly NOT a step toward
`coordinator_core.contract.apply_base` — this module does not import
`apply_base` and never will; see Negative-spec.

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C2g

Negative-spec:
    - Does NOT import or compose `coordinator_core.contract.apply_base`.
      `apply_base` is the cross-family Tier-B runner the two-lineage
      framing in the spec-backlinked plan deliberately abstains from; this
      module is the sibling family's OWN factor, one layer down from that
      abstention, not a bridge toward it.
    - Does NOT own `_execute_directives`/`_judgment_points_by_id` or any
      other directive-loop orchestration — each assembler's `apply.py`
      keeps authoring its own loop shape (e.g. `workday_complete`'s
      stdin-piping extension over `workweek_complete`'s plainer loop);
      only the gate predicate, the unrecognized-directive signal, and the
      exit-code ladder are common enough across every consumer to live
      here.
    - Does NOT read or validate `CONSUMES_MANIFEST` — that is each
      caller's own `brief()`-side concern, unrelated to the apply-side
      halt contract this module provides.
"""

from __future__ import annotations

import enum
from typing import Any, Type

from coordinator_core.contract.decision_object.envelope import extend_exit_codes

#: The four halt-contract exit-code members every ceremony-close assembler's
#: apply half raises beyond the fixed `SUCCESS = 0` anchor. Values are the
#: ceremony family's own convention (workday/workweek's identical ladders),
#: not `apply_base`'s or any other lineage's numbering.
CEREMONY_HALT_EXIT_CODES: dict[str, int] = {
    "HALTED_AT_JUDGMENT": 1,
    "DIRECTIVE_FAILED": 2,
    "TRANSPORT_FAIL": 3,
    "PARTIAL_MUTATION": 4,
}


def build_ceremony_halt_exit_codes(name: str) -> Type[enum.IntEnum]:
    """Build a ceremony-close assembler's apply-side exit-code `IntEnum`.

    Thin wrapper over `extend_exit_codes` pinned to the family's shared
    four-member halt ladder (`CEREMONY_HALT_EXIT_CODES`) so every consumer
    (`workday_complete.apply.WorkdayApplyExitCode`,
    `workweek_complete.apply.WorkweekApplyExitCode`, and
    `workstream_complete.apply`'s own enum) gets the SAME numbering under
    a caller-chosen class name, rather than each module hand-typing the
    four `int` literals and risking silent drift between them.
    """
    return extend_exit_codes(name, **CEREMONY_HALT_EXIT_CODES)


class UnrecognizedDirective(RuntimeError):
    """Raised when a directive names a `cli` outside a caller's closed
    dispatch table.

    A ceremony-close assembler's dispatch table is closed BY DESIGN — an
    unknown `cli` value is a brief/apply drift bug (the directive-producing
    half emitted an id the directive-consuming half never registered), not
    a runtime condition to route through the halt contract's ordinary
    per-directive `failed` bookkeeping. Callers raise this from their own
    `_dispatch_directive`-equivalent before ever invoking a handler, so the
    failure is visible at the exact directive whose `cli` is unrecognized.
    """


def _disposition_resolves_directive(
    judgment_point: dict[str, Any], chosen_value: str, directive_id: str
) -> bool:
    """The value-aware predicate: true iff `chosen_value` names a real entry
    in `judgment_point["dispositions"]` AND that entry's `resolves` list
    contains `directive_id` — never merely "a disposition was picked."""
    for entry in judgment_point.get("dispositions", []):
        if entry.get("value") == chosen_value:
            return directive_id in entry.get("resolves", [])
    return False


def _normalize_depends_on(value: Any) -> list[str]:
    """Byte-mirror of `coordinator_core.contract.apply_base.normalize_
    depends_on` — this module's own Negative-spec commits to never
    importing `apply_base`, so the None|str|list normalization is
    deliberately copied rather than shared. Keep in sync with the source
    if it ever changes: `depends_on` accepts `None`, a single id
    (`str`), or a list of ids per `schemas/decision-object.schema.json`
    `$defs/directive/properties/depends_on`, and the list form was the
    unhandled case that raised `TypeError: cannot use 'list' as a dict
    key` before this fix (Finding 3, `staff-eng` review 2026-07-27)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def assert_disjoint_dependency_namespaces(
    judgment_points_by_id: dict[str, dict[str, Any]],
    directive_ids: "frozenset[str] | set[str]",
) -> None:
    """`depends_on` is a UNION namespace — a member names either a live
    judgment-point id or a sibling directive id in the same envelope
    (Finding 1, `staff-eng` review 2026-07-27: four other assemblers
    already emit the directive-id form, consumed by
    `contract.apply_base.directive_gate_open`). `_directive_gate_open`
    below resolves a `depends_on` member by trying the judgment-point
    namespace first; that ordering is only safe because the two
    namespaces are asserted disjoint HERE, once per envelope, rather than
    left as an accident of lookup order. Call this once per brief-
    consumption (not per directive) before running the gate."""
    overlap = set(judgment_points_by_id) & set(directive_ids)
    assert not overlap, (
        f"judgment-point and directive id namespaces overlap: {sorted(overlap)!r} "
        "— depends_on cannot resolve an id that is ambiguous between the two"
    )


def _directive_gate_open(
    directive: dict[str, Any],
    judgment_points_by_id: dict[str, dict[str, Any]],
    decisions: dict[str, Any],
    directive_ids: "frozenset[str] | set[str]" = frozenset(),
) -> bool:
    """True iff `directive` is resolved-to-fire this pass. `depends_on`
    (normalized via `_normalize_depends_on` — `None`, a single id, or a
    list) is a UNION namespace; every member must independently clear
    before the directive fires:

    - A member naming a live judgment point gates on VALUE, exactly as
      before: `decisions[jp_id]["disposition"]` must be set AND that
      chosen disposition's own `resolves` list must name this directive.
    - A member naming a live sibling directive id in `directive_ids`
      (and NOT also a judgment point — see `assert_disjoint_dependency_
      namespaces`) does NOT gate here. This is deliberate, not an
      oversight: it matches `contract.apply_base.directive_gate_open`,
      which ignores any `depends_on` entry absent from `jp_by_id`
      (Finding 2, `staff-eng` review 2026-07-27). Producer readiness for
      a directive-id dependency is a DATA/ordering concern owned by the
      caller's own arg-token resolution (`_resolve_arg_tokens` in each
      `apply.py`), which already fails an unmet producer loud into
      `report["failed"]` (exit 2/4, DIRECTIVE_FAILED/PARTIAL_MUTATION).
      Gating on it here instead would route a failed producer through
      `report["blocked"]` -> HALTED_AT_JUDGMENT (exit 1, "a human must
      disposition a judgment point") — a state NO disposition can ever
      clear, since there is no judgment point to disposition. That is a
      permanent deadlock, not a halt, and this function must never
      produce it.
    - A member naming an id in NEITHER namespace fails CLOSED (unresolved,
      never auto-fire) — deliberately stricter than `apply_base`, which
      fails OPEN on the same case (silently ignores it, letting the
      directive fire as if the dependency were satisfied). A typo'd id is
      a producer bug; failing closed surfaces it as a stuck directive
      instead of masking it. This divergence from `apply_base` is
      intentional and covered by the cross-lineage contract test
      (`test_apply_halt.py::test_cross_lineage_agreement_and_divergence`)."""
    for dep in _normalize_depends_on(directive.get("depends_on")):
        judgment_point = judgment_points_by_id.get(dep)
        if judgment_point is not None:
            decision = decisions.get(dep)
            if not isinstance(decision, dict):
                return False
            chosen_value = decision.get("disposition")
            if not chosen_value:
                return False
            if not _disposition_resolves_directive(judgment_point, chosen_value, directive["id"]):
                return False
            continue
        if dep in directive_ids:
            # Ordering/data dependency — never gates here. See docstring.
            continue
        # Unknown in both namespaces — fail closed (deliberately stricter
        # than apply_base's fail-open on this same case).
        return False
    return True
