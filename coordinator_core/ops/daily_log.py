"""``changelog.daily_log``: the day's completed plans, ranked by sizing, with no model call.

COMPUTE_ONLY. Window defaults to the 24h ending now; ``since``/``until`` (ISO-8601 or epoch
seconds) override it. Rows come from ``daily_log_source.plans_completed_result`` (example-retrieval-repo view,
filesystem-scan fallback marked ``degraded``); this module only ranks and renders. It imports no agent, dispatch or model module.
"""
from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops import daily_log_source

DEFAULT_WINDOW_HOURS = 24


def _parse_moment(value: object) -> "datetime.datetime | int | float":
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.astimezone()


def render(entries: "list[dict]", since: int, until: int, degraded: Optional[dict] = None) -> str:
    """Markdown body: one bullet per ranked plan, ``sha`` pointing at the completing commit (``-`` when unknown)."""
    fmt = lambda e: datetime.datetime.fromtimestamp(e, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    lines = [f"## Plans completed {fmt(since)} to {fmt(until)}", ""]
    if degraded:
        lines += [f"_Degraded ({degraded['mode']}): {degraded['reason']}_", ""]
    if not entries:
        lines.append("None.")
    for row in entries:
        sha = (row.get("sha") or "-")[:10]
        lines.append(f"- [{row.get('sizing') or '?'}] `{row['plan']}` ({sha}): {row['summary_line']}")
    return "\n".join(lines) + "\n"


def daily_log(
    repo: "Path | str",
    since: "datetime.datetime | int | float | None" = None,
    until: "datetime.datetime | int | float | None" = None,
    *,
    view_db: "Path | str | None" = None,
    settings_home: "Path | str | None" = None,
) -> dict:
    """Ranked ``{plan, summary_line, sha, sizing}`` entries, rendered text and ``degraded`` for ``[since, until)``."""
    hi = daily_log_source._epoch(until if until is not None else datetime.datetime.now(datetime.timezone.utc))
    lo = daily_log_source._epoch(since) if since is not None else hi - DEFAULT_WINDOW_HOURS * 3600
    res = daily_log_source.plans_completed_result(repo, lo, hi, view_db=view_db, settings_home=settings_home)
    entries = daily_log_source.rank_by_sizing(res["rows"])
    return {
        "since": lo, "until": hi, "count": len(entries), "entries": entries,
        "degraded": res["degraded"], "text": render(entries, lo, hi, res["degraded"]),
    }


@register_op("changelog.daily_log")
async def _daily_log_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC changelog.daily_log handler.

    Optional params: ``since``, ``until`` (ISO-8601 or epoch seconds; default the 24h ending now).
    Returns ``{since, until, count, entries, degraded, text}``; ``entries`` are ranked by sizing descending.
    """
    if repo_root is None:
        return {"error": "changelog.daily_log: repo_root required"}
    try:
        since = _parse_moment(params["since"]) if params.get("since") is not None else None
        until = _parse_moment(params["until"]) if params.get("until") is not None else None
    except ValueError as exc:
        return {"error": f"changelog.daily_log: bad since/until: {exc}"}
    return await asyncio.to_thread(daily_log, main_worktree_root(repo_root), since, until)
