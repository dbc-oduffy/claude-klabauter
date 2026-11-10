"""Status-line field contract: Unimatrix name resolver and the one line formatter.

An unobserved field prints `unknown`, never 0 and never a full gauge.
"""
from __future__ import annotations

import json
from collections.abc import Iterable

from coordinator_core.agent_pipe.contract import peers_path

UNKNOWN = "unknown"
PARTIAL_MARK = "+ [coverage incomplete]"  # appended to the cost with no separator: "~$1.00+ [coverage incomplete]"


def resolve_name(vendor: str, ids: Iterable[str]) -> str | None:
    """Unimatrix name of the single `vendor` peer row whose target is in `ids`; None on zero or several matches.

    A registered session (address_version 1) whose name is among the matches wins.
    """
    wanted = {i for i in ids if isinstance(i, str) and i}
    if not wanted:
        return None
    try:
        value = json.loads(peers_path().read_text(encoding="utf-8"))
        rows = value.get("peers", []) if isinstance(value, dict) else value
        matches = sorted({row["name"] for row in rows if isinstance(row, dict)
                          and row.get("vendor") == vendor and row.get("target") in wanted
                          and isinstance(row.get("name"), str)})
        if len(matches) > 1:
            from coordinator_core.session import external_registry
            for sid in sorted(wanted):
                owned = external_registry.lookup(sid)
                if owned and owned.get("address_version") == 1 and owned.get("name") in matches:
                    return owned["name"]
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _gauge(pct: float) -> str:
    filled = max(0, min(5, round(pct / 20)))
    return "▰" * filled + "▱" * (5 - filled) + f" {pct:.0f}% ctx"


def format_line(name: str | None, before_compact_pct: float | None, usd: float | None,
                partial: bool = False, middle: tuple[str, ...] = ()) -> str:
    """`name · *middle · gauge · ~$usd`, each missing slot `unknown`; `partial` marks incomplete spend coverage."""
    cost = UNKNOWN if usd is None else f"~${usd:.2f}" + (PARTIAL_MARK if partial else "")
    context = UNKNOWN if before_compact_pct is None else _gauge(before_compact_pct)
    return " · ".join((name or UNKNOWN, *middle, context, cost))
