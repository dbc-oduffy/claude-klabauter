"""
coordinator_core.ops.ceremony.receipt_render — render a receipt's op_tail for a human.

Purpose: `op_tail` (receipt_schema.py) carries five partitions — acted, skipped,
failed, failed_critical, unknown — but the receipt is JSON on disk at
state/ceremony/<ceremony>-receipt.json and nothing renders it for a human. A step
that reports `unknown` is legible to code and invisible to the operator, which
restates the defect this module exists to close one layer up
(docs/plans/2026-08-29-steps-report-what-they-do-not-know.md § The gap).

This module is PURE — it takes an already-parsed receipt dict and returns
formatted text. It performs no disk I/O and no argument parsing; those live in
the CLI (coordinator/bin/render-ceremony-receipt.py).

Rendering contract:
  - `unknown` is legible indeterminacy, not failure (receipt_schema.py's own
    docstring). It renders under its own labelled section, distinct from
    `failed`/`failed_critical`, and is never folded into a total count.
  - Graceful-absent is the schema's governing rule and it governs rendering:
    a receipt with no `unknown` key at all (pre-existing receipt) renders a
    "not tracked by this receipt" line; a receipt WITH an empty `unknown: []`
    renders "(none)". These are different facts and must read as different
    text, never collapsed to the same output.
  - Every partition renders even when empty — an omitted section is a defect
    (it would restate the same "empty vs. absent" ambiguity the schema exists
    to avoid).

Negative spec — do NOT add a total/summary count line that mixes `unknown`
into the acted/skipped/failed arithmetic. A count field it is not is worse
than no count field: it invites reading "N items handled" as "N items fine."
"""

from __future__ import annotations

from typing import Any

from coordinator_core import timestamps

_PARTITION_ORDER: tuple[str, ...] = ("acted", "skipped", "failed", "failed_critical", "unknown")

_PARTITION_LABELS: dict[str, str] = {
    "acted": "ACTED",
    "skipped": "SKIPPED",
    "failed": "FAILED",
    "failed_critical": "FAILED (critical)",
    "unknown": "UNKNOWN — could not determine outcome, not a failure",
}


def render_op_tail(op_tail: dict[str, Any]) -> str:
    """Return a human-readable rendering of a single op_tail dict.

    Renders every partition in _PARTITION_ORDER. A partition key absent from
    op_tail (graceful-absent — `failed_critical`/`unknown` on a pre-existing
    receipt) renders "not tracked by this receipt"; a partition present but
    an empty list renders "(none)". These two states are different facts and
    render as different text on purpose — see module docstring.
    """
    lines: list[str] = []
    phase = op_tail.get("phase", "")
    if phase:
        lines.append(f"phase: {phase}")

    for key in _PARTITION_ORDER:
        label = _PARTITION_LABELS[key]
        if key not in op_tail:
            lines.append(f"  {label}: not tracked by this receipt")
            continue
        items = op_tail[key]
        if not isinstance(items, list):
            lines.append(f"  {label}: <malformed: not a list>")
            continue
        if not items:
            lines.append(f"  {label}: (none)")
            continue
        lines.append(f"  {label}:")
        for item in items:
            lines.append(f"    - {item}")

    return "\n".join(lines)


def _emitted_clause(emitted_at: Any) -> str:
    """The receipt's own stamp with the age a reader would otherwise subtract wrong.

    A receipt header exists so a printed report is self-describing; a UTC
    stamp alone is not, because the reader checks it against a local clock.
    An absent field keeps the header's `<unknown>` sentinel rather than being
    rendered as a stamp that was never there."""
    if not emitted_at:
        return "<unknown>"
    return timestamps.with_age(emitted_at)


def render_receipt_summary(receipt: dict[str, Any]) -> str:
    """Return a human-readable rendering of a whole receipt's op_tail.

    Includes the receipt's own identifying header (ceremony, phase, emitted_at)
    ahead of the op_tail rendering, so a printed report is self-describing
    without needing the source path repeated alongside it.
    """
    header_lines = [
        f"ceremony: {receipt.get('ceremony', '<unknown>')}",
        f"phase:    {receipt.get('phase', '<unknown>')}",
        f"emitted:  {_emitted_clause(receipt.get('emitted_at'))}",
    ]
    op_tail = receipt.get("op_tail")
    if not isinstance(op_tail, dict):
        header_lines.append("op_tail: MISSING or malformed — receipt carries no op_tail object")
        return "\n".join(header_lines)
    return "\n".join(header_lines) + "\n\n" + render_op_tail(op_tail)
