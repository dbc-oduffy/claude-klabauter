"""agy statusLine renderer: name from the registry, context unknown (window figures only), spend from `cost`."""
from __future__ import annotations

from coordinator_core.tui_status.fields import format_line, resolve_name

MIN_VISIBLE_USD = 0.005  # a cost below one rounded cent is unpriced, not "$0.00"


def usd_or_none(raw: object) -> float | None:
    """A finite cost at or above MIN_VISIBLE_USD, else None; a dict contributes `total_cost_usd`."""
    if isinstance(raw, dict):
        raw = raw.get("total_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw >= MIN_VISIBLE_USD and raw != float("inf") else None


def line(payload: dict) -> str:
    """The status line for one agy statusLine payload; the window percentages are not before-compact, so context is unknown."""
    name = resolve_name("agy", [payload.get("session_id"), payload.get("conversation_id")])
    return format_line(name, None, usd_or_none(payload.get("cost")))
