
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
    if not emitted_at:
        return "<unknown>"
    return timestamps.with_age(emitted_at)


def render_receipt_summary(receipt: dict[str, Any]) -> str:
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
