"""Stamps `**Last full audit:**` in the health ledger after a clean full pass, and nothing else."""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from coordinator_core.architecture_survey.contract import Decision
from coordinator_core.ops.check_arch_audit_staleness import (
    _STATE_ROOT_OVERRIDE_ENV,
    _claude_home,
    _claude_klabauter_root,
    _same_path,
)

LEDGER_NAME = "health-ledger.md"
_FULL_AUDIT_RE = re.compile(r"^(\*\*Last full audit:\*\*)( *)(\d{4}-\d{2}-\d{2})?(.*?)(\r?\n?)$")


class LedgerLineMissing(ValueError):
    """The ledger has no `**Last full audit:**` line to rewrite."""


def _ledger_path(repo_root: Path) -> Path:
    override = os.environ.get(_STATE_ROOT_OVERRIDE_ENV)
    if override:
        return Path(override) / LEDGER_NAME
    if _same_path(str(repo_root), _claude_home()):
        claude_klabauter = _claude_klabauter_root()
        if claude_klabauter is not None:
            return Path(claude_klabauter) / "state" / LEDGER_NAME
    return repo_root / "state" / LEDGER_NAME


def stamp_last_full_audit(
    repo_root: str | Path, *, mode: str, index_state: str, today: date
) -> Decision:
    """Set the ledger's `Last full audit` date to `today` on a full pass over a current index.

    Raises `LedgerLineMissing` when the ledger has no such line; never appends one.
    """
    if mode == "targeted":
        return Decision("ledger-stamp", "not-a-full-pass", "a targeted run never writes Last full audit", f"mode={mode}")
    if index_state == "behind":
        return Decision("ledger-stamp", "index-behind", "a pass over stale structure is not a full audit", f"index_state={index_state}")

    path = _ledger_path(Path(repo_root))
    with path.open("r", encoding="utf-8", newline="") as fh:
        lines = fh.read().splitlines(keepends=True)
    for i, raw in enumerate(lines):
        match = _FULL_AUDIT_RE.match(raw)
        if match:
            old = match.group(3) or ""
            new = today.isoformat()
            tail = match.group(4)
            if not old and tail and not tail[0].isspace():
                tail = " " + tail
            lines[i] = f"{match.group(1)} {new}{tail}{match.group(5)}"
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write("".join(lines))
            return Decision(
                "ledger-stamp",
                "stamped",
                "full pass over a current index moves Last full audit to today",
                f"{old or 'none'} -> {new}",
            )
    raise LedgerLineMissing(f"no '**Last full audit:**' line in {path}")
