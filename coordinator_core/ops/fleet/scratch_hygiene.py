"""fleet.scratch_hygiene — purge fleet scratch/temp and nag scratch-hold.

The op result carries ``records`` (the contract's JSON objects, ending with the summary record, which the
caller prints one per line), ``contract_exit`` (0/1/2 per the contract) and ``exit_code`` (0, or 2 for
bad input), so a findings report never reads as an in-band failure.

Negative-spec: deletes nothing unless ``apply`` is true; never touches scratch-hold, ``_fleet`` or
another repo's Temp subtree; spawns no process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet.scratch_hygiene_apply import apply_purge
from coordinator_core.ops.fleet.scratch_hygiene_nag import hold_nag, hold_pending_nag
from coordinator_core.ops.fleet.scratch_hygiene_plan import _Context, classify_entry, plan_purge
from coordinator_core.ops.fleet.scratch_hygiene_records import contract_exit, summary_record

CADENCES = ("workday_start", "workday_complete", "workweek_start", "workweek_complete", "distill")
_NAG_CADENCES = ("workweek_start", "workweek_complete")


def _bad(message: str) -> dict[str, Any]:
    return {"error": message, "records": [], "contract_exit": 2, "exit_code": 2}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def run_hygiene(params: dict) -> dict[str, Any]:
    """Validate ``params`` and produce the op result; synchronous, in-process, zero spawns."""
    raw_root = params.get("repo_root")
    if not isinstance(raw_root, str) or not raw_root:
        return _bad("repo_root is required")
    repo_root = Path(raw_root)
    if not repo_root.is_dir():
        return _bad(f"repo_root is not an existing directory: {raw_root}")
    cadence = params.get("cadence")
    if cadence not in CADENCES:
        return _bad(f"cadence must be one of {', '.join(CADENCES)}")
    apply = params.get("apply", False)
    nag_only = params.get("nag_only", False)
    if not isinstance(apply, bool) or not isinstance(nag_only, bool):
        return _bad("apply and nag_only must be bools")
    quiescence = _number(params.get("quiescence_hours", 24))
    if quiescence is None:
        return _bad("quiescence_hours must be a non-negative number")
    verify_copy = params.get("verify_copy")
    if verify_copy is not None:
        if not (
            isinstance(verify_copy, dict)
            and isinstance(verify_copy.get("source"), str)
            and isinstance(verify_copy.get("dest"), str)
        ):
            return _bad("verify_copy must be {source, dest} strings")
        verify_copy = {"source": verify_copy["source"], "dest": verify_copy["dest"]}

    records: list[dict[str, Any]] = []
    applied = False
    if not nag_only:
        records = plan_purge(repo_root, quiescence_hours=quiescence, verify_copy=verify_copy)
        if apply:
            # One registry read for the whole apply pass, not one per entry.
            recheck_ctx = _Context()
            records = apply_purge(
                repo_root,
                records,
                recheck=lambda entry: classify_entry(
                    entry,
                    repo_root=repo_root,
                    quiescence_hours=quiescence,
                    verify_copy=verify_copy,
                    _ctx=recheck_ctx,
                ),
            )
            applied = True
    if nag_only or cadence in _NAG_CADENCES:
        records = records + hold_nag(repo_root) + hold_pending_nag(repo_root)
    records.append(summary_record(records, applied=applied))
    return {"records": records, "contract_exit": contract_exit(records), "exit_code": 0}


@register_op("fleet.scratch_hygiene")
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.scratch_hygiene — keyed on the ``repo_root`` param (_OP_KEY_SCOPE="none"), not the caller's tree."""
    return await asyncio.to_thread(run_hygiene, dict(params or {}))
