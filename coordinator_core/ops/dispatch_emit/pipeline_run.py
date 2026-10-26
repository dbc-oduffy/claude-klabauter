"""Resolve an emitted research-pipeline run and append revisions to its revisions file.

A pipeline run's stages read ``revisions_rel(run_id)`` at stage start when the emitted script
carries ``REVISIONS_MARKER``; ``workflow.revise`` appends here.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_append import append_line
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT, RUN_ID_PREFIX
from coordinator_core.session.declared_writes import declare_write

__all__ = [
    "REVISIONS_MARKER", "FINISHED_REL", "emitted_pipeline_run", "revisions_rel", "finished_rel",
    "pipeline_run_state", "reads_revisions", "append_revision",
]

#: The preamble sentence an emitted pipeline script carries when its stages read the revisions file.
REVISIONS_MARKER = "Read the run's revisions file before acting; its newest entries override earlier scope."

_EMITTED_SUFFIX = ".workflow.mjs.emitted.json"


def emitted_pipeline_run(root: Path, run_id: str) -> Optional[Path]:
    """Script path of an emitted pipeline run under ``root``, or None (wrong prefix, unsafe id, no receipt)."""
    if not isinstance(run_id, str) or not run_id.startswith(RUN_ID_PREFIX) or not safe_id(run_id):
        return None
    base = Path(root) / RUN_DIR_ROOT
    receipt = contained_path(base / f"{run_id}{_EMITTED_SUFFIX}", [base])
    if receipt is None or not receipt.is_file():
        return None
    return base / f"{run_id}.workflow.mjs"


def revisions_rel(run_id: str) -> str:
    """Repo-relative path of the run's revisions file."""
    return f"{RUN_DIR_ROOT}/{run_id}/revisions.md"


#: Terminal marker the last composed stage writes under the run dir; its presence means the run finished.
FINISHED_REL = "finished.md"


def finished_rel(run_id: str) -> str:
    """Repo-relative path of the run's terminal marker."""
    return f"{RUN_DIR_ROOT}/{run_id}/{FINISHED_REL}"


def pipeline_run_state(root: Path, run_id: str) -> str:
    """"unknown" (no receipt or bad id), "finished" (terminal marker present) or "live".

    Trap: "live" means no marker yet, not running; a failed final stage never writes it.
    """
    if emitted_pipeline_run(root, run_id) is None:
        return "unknown"
    return "finished" if (Path(root) / finished_rel(run_id)).is_file() else "live"


def reads_revisions(script: str) -> bool:
    """True when the emitted script text carries ``REVISIONS_MARKER``."""
    return REVISIONS_MARKER in script


def append_revision(root: Path, run_id: str, text: str, sender: str, kind: str) -> Path:
    """Append one stamped block to the run's revisions file in a single write; returns the file path."""
    path = Path(root) / revisions_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = re.sub(r"(?m)^## ", r"\\## ", str(text).strip("\n"))
    block = f"## {stamp} from {sender} ({kind})\n\n{body}\n\n"
    append_line(path, block.encode("utf-8"))
    declare_write(path)
    return path
