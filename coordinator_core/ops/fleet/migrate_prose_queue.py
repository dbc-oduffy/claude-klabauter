"""fleet.migrate_prose_queue — convert state/<family>.md prose queues into state/<family>/*.yaml.

params: {repo_root: str (required, must equal the caller's toplevel), family: non-empty
list of improvement-queue|bug-backlog|debt-backlog, dry_run: bool (required)}.
candidate_ids is refused. Params are validated here, not by _common.validate_params
(which demands candidate_ids and mode). Own-tree only: a repo_root other than the
engine-supplied toplevel is refused before any file is read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet import _prose_queue_run
from coordinator_core.ops.fleet._prose_queue_contract import FAMILIES


def _refuse(error: str) -> dict:
    return {"exit_code": 2, "error": error}


@register_op("fleet.migrate_prose_queue")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """repo_root arg is the toplevel delivered by _OP_KEY_SCOPE="show_top"."""
    if "candidate_ids" in params:
        return _refuse("'candidate_ids' is not accepted; act covers each whole family file")

    param_root = params.get("repo_root")
    if not isinstance(param_root, str) or not param_root.strip():
        return _refuse("'repo_root' is required (non-empty string)")

    families = params.get("family")
    if not isinstance(families, list) or not families:
        return _refuse("'family' is required (non-empty list)")
    for fam in families:
        if not isinstance(fam, str) or fam not in FAMILIES:
            return _refuse(f"unknown family {fam!r}; expected one of {list(FAMILIES)}")

    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return _refuse(f"'dry_run' is required and must be bool, got {type(dry_run).__name__!r}")

    if repo_root is None:
        return {"exit_code": 1, "error": "engine toplevel unavailable (keying misconfiguration)"}
    if Path(param_root).resolve() != Path(repo_root).resolve():
        return {
            "exit_code": 1,
            "error": f"'repo_root' {param_root!r} differs from the caller toplevel {str(repo_root)!r}; "
            "the op migrates its own tree only",
        }

    return await asyncio.to_thread(_prose_queue_run.run_migration, Path(repo_root).resolve(), list(families), dry_run)
