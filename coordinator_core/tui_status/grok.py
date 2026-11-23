"""Grok `[ui.status_line] type = "command"` renderer: peer name, pre-compact budget used, spend."""
from __future__ import annotations

from coordinator_core.tui_status.agy import usd_or_none
from coordinator_core.tui_status.fields import UNKNOWN, format_line, resolve_name

_CELLS = 5


def _pct(raw: object) -> float | None:
    return None if isinstance(raw, bool) or not isinstance(raw, (int, float)) else float(raw)


def before_compact(remaining: object, threshold: object) -> float | None:
    """`remaining - (100 - threshold)` clamped at 0; None unless both percentages were observed.

    Codex still reads this. The Grok footer uses `budget_used`.
    """
    remaining, threshold = _pct(remaining), _pct(threshold)
    if remaining is None or threshold is None:
        return None
    return max(0.0, remaining - (100 - threshold))


def budget_used(remaining: object, threshold: object) -> float | None:
    """Percent of the pre-compaction budget already used: `(100 - remaining) / threshold * 100`.

    0 when the window is full, 100 when used reaches the compact threshold. Clamped to that
    range. None unless both percentages were observed and the threshold is positive.
    """
    remaining, threshold = _pct(remaining), _pct(threshold)
    if remaining is None or threshold is None or threshold <= 0:
        return None
    used = max(0.0, 100.0 - remaining)
    return min(100.0, used / threshold * 100.0)


def _ascii_context(pct: float) -> str:
    """Filled `#` cells and the percent. No empty cells, so the footer stays selectable text."""
    filled = max(0, min(_CELLS, round(pct / (100 / _CELLS))))
    cells = "#" * filled
    return f"{cells} {pct:.0f}% ctx" if cells else f"{pct:.0f}% ctx"


def line(payload: dict) -> str:
    """The status line for one Grok statusLine payload.

    Context is the rising pre-compaction budget, drawn with ASCII cells. `format_line`'s
    block gauge is not used, so an observed name, context, and cost never include `unknown`
    or an unfilled box.
    """
    window = payload.get("context_window")
    window = window if isinstance(window, dict) else {}
    cost = payload.get("cost")
    # Grok nests the threshold under context_window; the top-level key is the older payload shape.
    threshold = window.get("auto_compact_threshold_percent")
    if threshold is None:
        threshold = payload.get("auto_compact_threshold_percent")
    name = resolve_name("grok", [payload.get("session_id")])
    usd = usd_or_none(cost.get("total_cost_usd") if isinstance(cost, dict) else None)
    pct = budget_used(window.get("remaining_percentage"), threshold)
    if pct is None:
        return format_line(name, None, usd)
    cost_text = UNKNOWN if usd is None else f"~${usd:.2f}"
    return " · ".join((name or UNKNOWN, _ascii_context(pct), cost_text))
