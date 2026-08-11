"""
coordinator_core.ops.handoff_carry_gate — the disposition gate for
`## Carried Forward` items.

Purpose: an item may be carried forward indefinitely — depth is not a defect,
and nothing here counts hops. What this gate refuses is an item whose STATE is
undeclared: a missing `carry_id` (identity cannot be threaded), an
unrecognized `disposition`, or a terminal disposition with no
`disposition_detail` naming the closure reason, spinoff target, or concrete
blocking condition. A bare label is the same "operator remembers" shape the
field exists to remove.

Identity across re-wording: each carried item carries a stable `carry_id`
(minted once, threaded byte-identical hop to hop — see
Example-doctrine-repo coordinator/schemas/handoff.schema.json `carried_items`). The gate
keys EXCLUSIVELY on `carry_id`, never on `description` text, so rephrasing an
item never makes it look "new".

Entry point: `evaluate_gate(items)` — returns a `GateResult`; `main()` maps a
non-ok result to exit 1 and a malformed/unreadable handoff to exit 2.

Spec backlink: example-doctrine-repo coordinator/schemas/handoff.schema.json
    `carried_items` field, coordinator/skills/handoff/SKILL.md
    § Cascading unresolved items.

Negative-spec:
    - Does NOT count carries and does NOT refuse on carry depth. Carries are
      indefinite by design (PM ruling, 2026-08-06): the `carry_count` field,
      the third-carry refusal, the `advance()` counter, and the
      `COORDINATOR_OVERRIDE_CARRY_GATE` escape hatch that existed only to
      override that refusal are all gone. Do not reintroduce a depth limit
      here — an item carried ten times with a declared disposition is a
      legitimate state, not a violation to be re-fenced.
    - Does NOT parse `## Carried Forward` prose. This module operates ONLY on
      the structured `carried_items` frontmatter array; the prose section
      remains human-authored display text derived FROM this structure, not a
      second source of truth the gate reads.
    - Does NOT key on `description` — see identity note above.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

_SANCTIONED_DISPOSITIONS = frozenset({"carried", "closed", "spun_off", "blocked"})
_TERMINAL_DISPOSITIONS = frozenset({"closed", "spun_off", "blocked"})
_PROG = "handoff-carry-gate"


class CarryGateError(Exception):
    """Raised on any refusal or fail-loud ambiguity.

    `violations` holds one human-readable line per offending item; the
    exception's `str()` joins them, so a bare `print(exc)` at a CLI call site
    already gives a complete, actionable report.
    """

    def __init__(self, violations: List[str]):
        self.violations = violations
        super().__init__("\n".join(violations))


@dataclass
class GateResult:
    """Non-raising result form for callers that want a value, not an exception."""

    ok: bool
    violations: List[str] = field(default_factory=list)


def evaluate_gate(items: List[Mapping[str, Any]]) -> GateResult:
    """Evaluate a `carried_items` list for declared, unambiguous state.

    Fail-loud rule (never fail open): an item whose identity or disposition
    cannot be determined is ALWAYS a violation — "cannot tell" is refused, not
    assumed fine. Carry depth is not evaluated at all.
    """
    violations: List[str] = []

    for idx, raw_item in enumerate(items):
        label = _item_label(raw_item, idx)

        carry_id = raw_item.get("carry_id") if isinstance(raw_item, Mapping) else None
        if not carry_id or not isinstance(carry_id, str):
            violations.append(
                f"{label}: missing or non-string carry_id — cannot verify identity "
                "across handoffs; refusing rather than assuming this is a new item."
            )
            continue

        disposition = raw_item.get("disposition") if isinstance(raw_item, Mapping) else None
        if disposition not in _SANCTIONED_DISPOSITIONS:
            violations.append(
                f"{label} (carry_id={carry_id}): disposition {disposition!r} is not one "
                f"of {sorted(_SANCTIONED_DISPOSITIONS)} — cannot evaluate the gate for "
                "an unrecognized disposition."
            )
            continue

        if disposition in _TERMINAL_DISPOSITIONS:
            detail = raw_item.get("disposition_detail") if isinstance(raw_item, Mapping) else None
            if not detail or not str(detail).strip():
                violations.append(
                    f"{label} (carry_id={carry_id}): disposition {disposition!r} requires "
                    "a non-empty disposition_detail naming the closure reason, spinoff "
                    "target, or concrete blocking condition — a bare disposition label "
                    "is the same 'operator remembers' shape this field exists to remove."
                )

    return GateResult(ok=not violations, violations=violations)


def _item_label(raw_item: Any, idx: int) -> str:
    if isinstance(raw_item, Mapping):
        desc = raw_item.get("description")
        if desc:
            return f"carried_items[{idx}] ({desc!r})"
    return f"carried_items[{idx}]"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def read_carried_items(handoff_path: str) -> List[Dict[str, Any]]:
    """Read and parse the `carried_items` frontmatter array off `handoff_path`.

    Public (no leading underscore) because `coordinator_core.baton_assemble.apply`'s
    `_dispatch_handoff_carry_gate` calls this directly rather than re-implementing
    the same read/parse/validate sequence -- see Review:
    coordinatorcode-reviewer-625ab891 finding 1 for why the duplicate existed and
    why this is the fix.
    """
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    text = Path(handoff_path).read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        raise CarryGateError([f"{handoff_path}: no parseable YAML frontmatter block"])
    fm = yaml.safe_load(split.fm_text) or {}
    items = fm.get("carried_items") or []
    if not isinstance(items, list):
        raise CarryGateError([f"{handoff_path}: carried_items is not a list (got {type(items).__name__})"])
    return items


def main(argv: List[str]) -> int:
    """CLI entry: `handoff-carry-gate check <handoff-path>`."""
    if not argv or argv[0] != "check" or len(argv) != 2:
        # Review: coordinatorcode-reviewer-6b7a876d — reject trailing argv
        # (e.g. a stale --override-reason) rather than silently swallowing it.
        print(f"{_PROG}: usage: {_PROG} check <handoff-path>", file=sys.stderr)
        return 2

    handoff_path = argv[1]

    if not os.path.exists(handoff_path):
        print(f"{_PROG}: path not found: {handoff_path}", file=sys.stderr)
        return 2

    try:
        items = read_carried_items(handoff_path)
    except CarryGateError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 2

    result = evaluate_gate(items)
    if result.ok:
        return 0

    for line in result.violations:
        print(f"{_PROG}: REFUSED: {line}", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
