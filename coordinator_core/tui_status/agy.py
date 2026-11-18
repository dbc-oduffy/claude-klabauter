"""agy statusLine renderer: name from the registry, context unknown (no established compaction threshold), spend priced from lifetime tokens."""
from __future__ import annotations

from coordinator_core.tui_status.fields import format_line, resolve_name
from coordinator_core.usage.price_table import estimate_usd

MIN_VISIBLE_USD = 0.005  # a cost below one rounded cent is unpriced, not "$0.00"


def usd_or_none(raw: object) -> float | None:
    """A finite cost at or above MIN_VISIBLE_USD, else None; a dict contributes `total_cost_usd`."""
    if isinstance(raw, dict):
        raw = raw.get("total_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) if raw >= MIN_VISIBLE_USD and raw != float("inf") else None


def _model_id(payload: dict) -> str | None:
    model = payload.get("model")
    if isinstance(model, dict):
        model = model.get("id") or model.get("display_name")
    return model if isinstance(model, str) else None


def priced_lifetime_usd(payload: dict) -> float | None:
    """API-equivalent USD of the payload's lifetime `context_window` token totals; None when the model is unpriced or no tokens were observed.

    The totals carry no cache split, thinking-token share or per-request prompt size, so the caller
    marks the figure partial.
    """
    window = payload.get("context_window")
    model = _model_id(payload)
    if not isinstance(window, dict) or model is None:
        return None
    usd = estimate_usd(model, {"input_tokens": window.get("total_input_tokens"),
                               "output_tokens": window.get("total_output_tokens")})
    return usd_or_none(usd)


def line(payload: dict) -> str:
    """The status line for one agy statusLine payload.

    Context is unknown: the window percentages are not before-compact and the payload carries no
    compaction threshold. Spend is the payload's `cost` when present, else the lifetime tokens
    priced at Gemini list rates and marked partial.
    """
    name = resolve_name("agy", [payload.get("session_id"), payload.get("conversation_id")])
    cost = usd_or_none(payload.get("cost"))
    if cost is not None:
        return format_line(name, None, cost)
    return format_line(name, None, priced_lifetime_usd(payload), partial=True)
