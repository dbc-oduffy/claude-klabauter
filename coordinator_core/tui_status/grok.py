"""Grok `[ui.status_line] type = "command"` renderer: peer name, context before compact, spend."""
from __future__ import annotations

from coordinator_core.tui_status.agy import usd_or_none
from coordinator_core.tui_status.fields import format_line, resolve_name


def _pct(raw: object) -> float | None:
    return None if isinstance(raw, bool) or not isinstance(raw, (int, float)) else float(raw)


def before_compact(remaining: object, threshold: object) -> float | None:
    """`remaining - (100 - threshold)` clamped at 0; None unless both percentages were observed."""
    remaining, threshold = _pct(remaining), _pct(threshold)
    if remaining is None or threshold is None:
        return None
    return max(0.0, remaining - (100 - threshold))


def line(payload: dict) -> str:
    """The status line for one Grok statusLine payload."""
    window = payload.get("context_window")
    window = window if isinstance(window, dict) else {}
    cost = payload.get("cost")
    return format_line(
        resolve_name("grok", [payload.get("session_id")]),
        before_compact(window.get("remaining_percentage"), payload.get("auto_compact_threshold_percent")),
        usd_or_none(cost.get("total_cost_usd") if isinstance(cost, dict) else None))
