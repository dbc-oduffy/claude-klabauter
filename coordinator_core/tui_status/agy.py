"""agy statusLine renderer: name from the registry, context from window remaining/used percentage, spend from cost, lifetime tokens, or token total."""
from __future__ import annotations

from coordinator_core.tui_status.fields import UNKNOWN, _gauge, format_line, resolve_name
from coordinator_core.usage.price_table import estimate_usd

MIN_VISIBLE_USD = 0.005  # a cost below one rounded cent is unpriced, not "$0.00"


def _pct(raw: object) -> float | None:
    return None if isinstance(raw, bool) or not isinstance(raw, (int, float)) else float(raw)


def context_pct(payload: dict) -> float | None:
    """Remaining context percentage from `context_window` (or top level); uses `before_compact` when a threshold is present."""
    window = payload.get("context_window")
    window = window if isinstance(window, dict) else payload
    remaining = _pct(window.get("remaining_percentage"))
    if remaining is None:
        used = _pct(window.get("used_percentage"))
        if used is not None:
            remaining = max(0.0, min(100.0, 100.0 - used))
    if remaining is None:
        return None
    threshold = window.get("auto_compact_threshold_percent")
    if threshold is None:
        threshold = payload.get("auto_compact_threshold_percent")
    if threshold is not None:
        from coordinator_core.tui_status.grok import before_compact
        return before_compact(remaining, threshold)
    return max(0.0, min(100.0, remaining))


def _token_total_label(payload: dict) -> str | None:
    window = payload.get("context_window")
    if not isinstance(window, dict):
        return None
    inp = window.get("total_input_tokens")
    out = window.get("total_output_tokens")
    total = 0.0
    for v in (inp, out):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            total += float(v)
    if total <= 0:
        return None
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M tok"
    if total >= 1_000:
        return f"{total / 1_000:.1f}k tok"
    return f"{int(total)} tok"


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
    """The status line for one agy statusLine payload."""
    name = resolve_name("agy", [payload.get("session_id"), payload.get("conversation_id")])
    ctx = context_pct(payload)
    cost = usd_or_none(payload.get("cost"))
    if cost is not None:
        return format_line(name, ctx, cost)
    priced = priced_lifetime_usd(payload)
    if priced is not None:
        return format_line(name, ctx, priced, partial=True)
    tok = _token_total_label(payload)
    if tok is not None:
        return " · ".join((name or UNKNOWN, UNKNOWN if ctx is None else _gauge(ctx), tok))
    return format_line(name, ctx, None)
