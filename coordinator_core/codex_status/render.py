"""A complete companion bar; native Codex footer remains built-in items only."""
from __future__ import annotations

from coordinator_core.tui_status.fields import UNKNOWN, format_line, resolve_name


NATIVE_FOOTER = ["thread-name", "model-with-reasoning", "context-remaining"]


def registered_alias(sid: str) -> str | None:
    return resolve_name("codex", [sid])


def render(value: dict) -> str:
    """Codex status line; context before compact is unknown unless `value` carries an observed `before_compact_percent`."""
    partial = bool(value.get("warnings") or value.get("unpriced_events") or
                   not all(value.get("coverage", {}).values()) or not value.get("backlog_complete", False))
    usd = value["usd"] if value.get("priced_events") else None
    return format_line(value.get("alias"), value.get("before_compact_percent"), usd, partial,
                       (value.get("model") or UNKNOWN, value.get("effort") or UNKNOWN))
