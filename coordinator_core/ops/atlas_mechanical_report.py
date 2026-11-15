"""
coordinator_core.ops.atlas_mechanical_report — JSON-RPC "update_docs.atlas_mechanical_report".

Purpose: the single engine entry for /update-docs atlas Step 0. Runs
`architecture_survey.mechanical_report.produce` in the warm engine, which writes the
atlas-mechanical-layer report, and replies with where it landed and how big it is.

NEGATIVE-SPEC:
  - The report is assembled by `produce`; this op never rebuilds or edits it.
  - No shell-out and no hardcoded output root: the default target is `produce`'s
    repo-relative `scratch/atlas-mechanical/<date>-report.json`.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Optional

from coordinator_core.architecture_survey import mechanical_report
from coordinator_core.ipc import register_op

_DEFAULT_REL = Path("scratch") / "atlas-mechanical"


@register_op("update_docs.atlas_mechanical_report")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    # produce() reads the atlas and fetches structure synchronously; off-loop so dispatch's wait_for can fire.
    return await asyncio.to_thread(_run, params, repo_root)


def _run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "update_docs.atlas_mechanical_report" handler — writes the report, COMPUTE_ONLY.

    Optional params: `repo_root` (default: the invoking repo), `out` (report path; default
    `<repo_root>/scratch/atlas-mechanical/<today>-report.json`).

    Returns `{"exit_code": 0, "report_path": str, "pages_changed": int, "dead_paths": int,
    "tokens": int, "bytes": int, "pages": int}` where `tokens` is the report's
    `narrative_payload`. Refusal: `{"exit_code": 2, "error": "not-a-git-repo"}`. Trap: no cwd git fallback -- the
    dispatcher always passes repo_root, and a rev-parse here would be the op's only spawn.
    """
    params = params or {}
    root = params.get("repo_root") or repo_root
    if not root or not Path(str(root)).is_dir():
        return {"exit_code": 2, "error": "not-a-git-repo"}
    root = Path(str(root)).resolve()
    out = params.get("out")
    stamp = date.today()
    report = mechanical_report.produce(root, out, today=stamp)
    target = Path(out) if out else root / _DEFAULT_REL / f"{stamp.isoformat()}-report.json"
    tokens = report["tokens"]
    return {
        "exit_code": 0,
        "report_path": str(target),
        "pages_changed": len(report["pages_changed"]),
        "dead_paths": len(report["dead_paths"]),
        "tokens": tokens["narrative_payload"],
        "bytes": tokens["bytes"],
        "pages": tokens["pages"],
    }
