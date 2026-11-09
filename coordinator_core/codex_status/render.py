"""A complete companion bar; native Codex footer remains built-in items only."""
from __future__ import annotations

from coordinator_core.agent_pipe.contract import peers_path
import json


NATIVE_FOOTER = ["thread-name", "model-with-reasoning", "context-remaining"]


def registered_alias(sid: str) -> str | None:
    try:
        value = json.loads(peers_path().read_text(encoding="utf-8"))
        rows = value.get("peers", []) if isinstance(value, dict) else value
        matches = sorted({row["name"] for row in rows if isinstance(row, dict)
                          and row.get("vendor") == "codex" and row.get("target") == sid and isinstance(row.get("name"), str)})
        from coordinator_core.session import external_registry
        owned = external_registry.lookup(sid)
        if owned and owned.get("address_version") == 1 and owned.get("name") in matches:
            return owned["name"]  # Canonical SSOT preference, verified against the actual projected route.
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def render(value: dict) -> str:
    alias = value.get("alias") or "route?"
    model, effort = value.get("model") or "model?", value.get("effort") or "effort?"
    percent = value.get("remaining_percent")
    gauge = "context?"
    if percent is not None:
        filled = max(0, min(5, round(percent / 20)))
        gauge = "▰" * filled + "▱" * (5 - filled) + f" {percent:.0f}% ctx"
    partial = bool(value.get("warnings") or value.get("unpriced_events") or
                   not all(value.get("coverage", {}).values()) or not value.get("backlog_complete", False))
    cost = f"~${value['usd']:.2f}" if value.get("priced_events") else "~$?"
    if partial:
        cost += "+ [coverage incomplete]"
    return " · ".join((alias, model, effort, gauge, cost))
