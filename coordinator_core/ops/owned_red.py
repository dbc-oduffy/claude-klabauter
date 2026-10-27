"""Reader for a run's owned_red record (`workflow.revise owned_red`): failing test ids another run owns."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.requirements_ledger import OWNED_RED_FILE, run_dir


def read_owned_red(repo_root: "Path | str", run_key: str) -> dict[str, str]:
    """Map nodeid -> owner_run_id from the run's owned_red.jsonl; later lines win.

    Missing file, unreadable file, malformed lines and rows lacking a nodeid or owner yield no entry.
    Raises LedgerRefused on an unsafe run_key.
    """
    path = run_dir(Path(repo_root), run_key) / OWNED_RED_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        nodeid, owner = row.get("nodeid"), row.get("owner_run_id")
        if isinstance(nodeid, str) and nodeid and isinstance(owner, str) and owner:
            out[nodeid] = owner
    return out
