"""Per-stage receipts of the ask run (P-B): write one, read them back, list the mandatory ones missing.

A receipt is `<run_dir>/receipts/<stage>.json` with the `Receipt` fields of run_truth_contract; `run_dir`
is the tracked `state/warp/<run_id>` directory. Pure file reads and one atomic write, no git, no
subprocess. Importing this module writes nothing: a stage leg of the composed ask script calls the
`dispatch.write_receipt` op.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import run_truth_contract as rtc
from coordinator_core.session import record_homes

OP_NAME = "dispatch.write_receipt"
RECEIPTS_DIR = "receipts"


def receipt_path(run_dir, stage: str) -> Path:
    return Path(run_dir) / RECEIPTS_DIR / f"{stage}.json"


def write_receipt(run_dir, stage: str, verdict: str, artifact: Optional[str], *, run_id: Optional[str] = None) -> Path:
    """Atomically write the stage's receipt (tmp then replace) and return its path.

    `run_id` defaults to the run_dir's name. A stage outside MANDATORY_STAGES is refused: a typo'd
    stage would otherwise leave the real one missing without a trace.
    """
    if stage not in rtc.MANDATORY_STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {rtc.MANDATORY_STAGES}")
    if not verdict:
        raise ValueError("a receipt needs a verdict")
    path = receipt_path(run_dir, stage)
    payload = {
        "stage": stage,
        "run_id": run_id or Path(run_dir).name,
        "verdict": verdict,
        "artifact": artifact,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_receipt(run_dir, stage: str) -> Optional[rtc.Receipt]:
    """The stage's receipt, or None when absent or malformed (a malformed receipt is a missing one)."""
    try:
        data = json.loads(receipt_path(run_dir, stage).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("stage") != stage or not data.get("verdict"):
        return None
    if any(k not in data for k in rtc.RECEIPT_FIELDS):
        return None
    return rtc.Receipt(**{k: data[k] for k in rtc.RECEIPT_FIELDS})


def present(run_dir) -> tuple[str, ...]:
    """Stages with a readable receipt, in MANDATORY_STAGES order."""
    return tuple(s for s in rtc.MANDATORY_STAGES if read_receipt(run_dir, s) is not None)


def missing(run_dir, arm: Optional[str], tshirt: Optional[str]) -> tuple[str, ...]:
    """Mandatory stages for this arm and size with no receipt. An unknown size or arm makes every
    stage mandatory (run_truth_contract.mandatory_stages)."""
    have = set(present(run_dir))
    return tuple(s for s in rtc.mandatory_stages(arm, tshirt) if s not in have)


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.write_receipt": write the stage receipt of `run_id` under state/warp."""
    params = params if isinstance(params, dict) else {}
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return {"ok": False, "error": "run_id must be a plain run id"}
    try:
        path = write_receipt(
            Path(record_homes.home_dir(str(repo_root), "warp")) / run_id,
            str(params.get("stage") or ""),
            str(params.get("verdict") or ""),
            params.get("artifact"),
            run_id=run_id,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": path.relative_to(Path(repo_root)).as_posix()}
