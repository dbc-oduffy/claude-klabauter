"""Record builders and exit mapping for the ``fleet.scratch_hygiene`` JSONL contract.

Key names, key order and the action enum are DoE's contract
(coordinator-content-repo ``coordinator/docs/wiki/scratch-and-temp-hygiene-engine-contract.md``); do not extend them.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

OP_NAME = "fleet.scratch_hygiene"

ACTIONS = (
    "deleted",
    "would-delete",
    "skipped-live",
    "skipped-unverified",
    "skipped-young",
    "skipped-link",
    "skipped-hold-pending",
)

FINDINGS = ("held", "missing-readme", "hold-pending")

#: Callers gate behaviour on these, not on op version: DoE passes ``apply: true`` only when
#: ``hold_pending_marker`` is present.
CAPABILITIES = ("hold_pending_marker",)


def repo_relative_path(path: Path | str, repo_root: Path | str | None = None) -> str:
    """Forward-slashed path, relative to ``repo_root`` when it lies under it, else as given."""
    p = Path(path)
    if repo_root is not None:
        try:
            p = p.relative_to(Path(repo_root))
        except ValueError:
            pass
    return PurePosixPath(*p.parts).as_posix() if p.parts else ""


def purge_record(
    repo: str,
    path: Path | str,
    action: str,
    *,
    bytes: int = 0,
    files: int = 0,
    age_days: float = 0.0,
    reason: str = "",
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"action {action!r} not in {ACTIONS}")
    return {
        "op": OP_NAME,
        "kind": "purge",
        "repo": repo,
        "path": repo_relative_path(path, repo_root),
        "action": action,
        "bytes": bytes,
        "files": files,
        "age_days": age_days,
        "reason": reason,
    }


def hold_nag_record(
    repo: str,
    path: Path | str,
    *,
    bytes: int = 0,
    age_days: float = 0.0,
    readme: str | None = None,
    finding: str = "held",
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    if finding not in FINDINGS:
        raise ValueError(f"finding {finding!r} not in {FINDINGS}")
    return {
        "op": OP_NAME,
        "kind": "hold-nag",
        "repo": repo,
        "path": repo_relative_path(path, repo_root),
        "bytes": bytes,
        "age_days": age_days,
        "readme": readme,
        "finding": finding,
    }


def summary_record(records: Iterable[Mapping[str, Any]], *, applied: bool) -> dict[str, Any]:
    """``entries`` counts purge and hold-nag records; ``bytes`` sums purge records that were or would be deleted."""
    entries = 0
    total = 0
    for rec in records:
        if rec.get("summary"):
            continue
        entries += 1
        if rec.get("kind") == "purge" and rec.get("action") in ("deleted", "would-delete"):
            total += int(rec.get("bytes", 0))
    return {
        "op": OP_NAME,
        "summary": True,
        "entries": entries,
        "bytes": total,
        "applied": applied,
        "capabilities": list(CAPABILITIES),
    }


def contract_exit(records: Iterable[Mapping[str, Any]]) -> int:
    """1 when any purge record is ``skipped-*`` or any nag finding is ``missing-readme`` or ``hold-pending``, else 0.

    Exit 2 (bad arguments) is the handler's, never derivable from records.
    """
    for rec in records:
        if rec.get("kind") == "purge" and str(rec.get("action", "")).startswith("skipped-"):
            return 1
        if rec.get("kind") == "hold-nag" and rec.get("finding") in ("missing-readme", "hold-pending"):
            return 1
    return 0
