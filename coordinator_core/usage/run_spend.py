"""Shared run-spend writer: spend_summary, ceremony_of, record_run.

Every ceremony's spend row is one file, state/spend/<YYYY-MM>/<run_id>.json, so concurrent
peers never share an append target. Writers pass the scan they already hold; scan_run runs at
most once per call. Background callers only: scan_run reads every agent transcript.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SPEND_SCHEMA = "ceremony-spend/1"
SPEND_DIR = "state/spend"

# Ordered (stem prefix, ceremony); first prefix match wins. The table is the contract.
_STEM_TABLE: tuple[tuple[str, str], ...] = (
    ("distill-harvest", "distill"),
    ("survey", "atlas"),
    ("plan-blitz", "review"),
    ("review-wave", "review"),
    ("engine-ask", "emit"),
    ("ask-", "emit"),
    ("warp-", "emit"),
    ("resume-script", "emit"),
    ("learn-lessons", "learn-lessons"),
    ("lessons", "learn-lessons"),
    ("update-docs", "update-docs"),
    ("research", "research"),
)
_EXTENSIONS = (".workflow.js", ".workflow.mjs", ".js", ".mjs")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def script_stem(script_path: Optional[str]) -> Optional[str]:
    """File name of script_path with the workflow/js extension stripped; None for no path."""
    if not script_path:
        return None
    name = os.path.basename(str(script_path).replace("\\", "/"))
    for ext in _EXTENSIONS:
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name or None


def ceremony_of(script_path: Optional[str]) -> str:
    """Ceremony name for a Workflow script path; `other` for an unknown stem or no path."""
    stem = script_stem(script_path)
    if stem is None:
        return "other"
    for prefix, ceremony in _STEM_TABLE:
        if stem.startswith(prefix):
            return ceremony
    return "other"


def spend_summary(scan: dict[str, Any]) -> dict[str, Any]:
    """{tokens, agents, usd, by_role, by_model} from a scan_run result; by_role verbatim."""
    total = scan.get("run_total") or {}
    return {
        "tokens": total.get("total_tokens", 0),
        "agents": len(scan.get("agents") or []),
        "usd": total.get("usd", 0.0),
        "by_role": scan.get("by_role") or {},
        "by_model": scan.get("by_model") or {},
    }


def record_run(
    wf_dir: str,
    repo_root: str,
    run_id: str,
    ceremony: str,
    *,
    scan: Optional[dict[str, Any]] = None,
    script_path: Optional[str] = None,
) -> dict[str, Any]:
    """Write the run's spend row; return {summary, spend_row} or {reason}. Never raises.

    script_stem on the row comes from script_path, so an `other` row stays attributable. The
    caller owns landing `spend_row`; the month dir is the write month, so a run written across a
    month boundary leaves one row per month.
    """
    try:
        if not run_id or not _SAFE_ID.match(run_id):
            return {"reason": "bad run_id"}
        if scan is None:
            from coordinator_core.usage.agent_usage import scan_run
            scan = scan_run(str(wf_dir))
        summary = spend_summary(scan)
        now = datetime.now(timezone.utc)
        rel = f"{SPEND_DIR}/{now.strftime('%Y-%m')}/{run_id}.json"
        row = {"schema": SPEND_SCHEMA, "run_id": run_id, "ceremony": ceremony,
               "script_stem": script_stem(script_path),
               "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **summary}
        from coordinator_core.session.claimed_write import replace_text
        dest = Path(repo_root) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        replace_text(dest, json.dumps(row, indent=2, sort_keys=True) + "\n")
        return {"summary": summary, "spend_row": rel}
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        return {"reason": f"{type(exc).__name__}: {exc}"}
