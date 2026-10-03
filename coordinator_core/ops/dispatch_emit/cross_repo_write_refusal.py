"""
coordinator_core.ops.dispatch_emit.cross_repo_write_refusal -- refuse a
spine whose declared ``writes:`` land in a sibling repo the terminal
commit cannot reach.

Purpose: friction item 2026-09-27 "cross-repo rows went uncommitted
silently". ``dispatch.terminal_commit`` (terminal_commit.py) issues
exactly ONE ``ceremony.commit_v2`` call, keyed on the caller's own
worktree (``repo_root``). A row whose ``writes:`` path resolves under a
SIBLING checkout -- a directory that sits next to ``repo_root`` and is
itself a git worktree (has its own ``.git``) -- can never land there: the
path does not exist relative to ``repo_root``, so ``terminal_commit``
silently drops it as "absent and untracked at HEAD" (its own
``dropped_absent`` contract) rather than raising. The edit sits on disk,
uncommitted, until a human notices.

This module is the smaller of the two correct fixes named in that
friction report: refuse the row AT EMIT TIME, naming the offending rows
and the sibling repo, rather than teaching ``terminal_commit`` to issue a
second ``ceremony.commit_v2`` call against a different worktree (which
would also need its own admission/session-claim story). A plan that
genuinely needs a cross-repo write still has one route: split it into two
plans, one per repo, and use ``external_gate`` to sequence them -- exactly
the convention ``spine_read.py`` already documents for cross-repo
blockers.

A path is outside ``repo_root`` when it is absolute (drive-letter or POSIX)
and not under ``repo_root``, when its normalised relative form escapes via
``..``, or when its first segment names a sibling git checkout.

Negative-spec: this module does not walk the tree or call ``git status``.
It is pure path arithmetic plus one targeted ``.git``-presence probe on the
sibling candidate a row itself named -- never a directory scan for what
"might" be a sibling repo.

``gated_rows`` is the gate ledger: it reads the ``exclusions`` ledger
``read_spine`` fills and never re-derives gating. It withholds, never refuses;
the rows land in ``StageManifest.gated``.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.ask_contract import GatedRow
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

_DRIVE_ABS = re.compile(r"^[A-Za-z]:/")


class CrossRepoWriteError(ValueError):
    pass


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _is_absolute(posix_path: str) -> bool:
    return posix_path.startswith("/") or bool(_DRIVE_ABS.match(posix_path))


def _under_root(posix_path: str, root_posix: str) -> bool:
    candidate = posixpath.normpath(posix_path)
    root = posixpath.normpath(root_posix)
    if _DRIVE_ABS.match(root):
        candidate, root = candidate.casefold(), root.casefold()
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def paths_outside_repo_root(paths: Iterable[str], repo_root: Path) -> list[str]:
    """The paths a terminal commit keyed on ``repo_root`` cannot reach, in
    input order: absolute and not under ``repo_root``, ``..``-escaping, or
    first-segment-sibling-repo."""
    root_posix = _posix(str(repo_root))
    repo_root = Path(repo_root)
    out: list[str] = []
    for raw in paths:
        posix = _posix(raw)
        if _is_absolute(posix):
            if not _under_root(posix, root_posix):
                out.append(raw)
            continue
        normalized = posixpath.normpath(posix)
        if normalized == ".." or normalized.startswith("../"):
            out.append(raw)
            continue
        segment = _first_segment(normalized)
        if segment is not None and _is_sibling_repo_dir(repo_root, segment):
            out.append(raw)
    return out


def _first_segment(path: str) -> Optional[str]:
    normalized = path.replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    return normalized.split("/", 1)[0]


def _is_sibling_repo_dir(repo_root: Path, segment: str) -> bool:
    """True when ``segment`` names a directory sitting next to
    ``repo_root`` (not ``repo_root`` itself) that is itself a git
    worktree -- the concrete, git-verifiable signal that a row's
    ``writes:`` path names a SIBLING repo by convention rather than a
    subdirectory of the emitting repo."""
    if segment == repo_root.name:
        return False
    candidate = repo_root.parent / segment
    if not candidate.is_dir():
        return False
    return (candidate / ".git").exists()


def check_cross_repo_writes(rows, repo_root: Optional[Path]) -> None:
    """Refuse emission if any row's ``writes:`` (or ``writes_under:``)
    resolves under a sibling repo's checkout, naming the offending rows
    and the repo. No-op when ``repo_root`` is ``None`` -- mirrors
    ``check_cross_plan_write_overlap``'s own posture, since there is no
    worktree to compare a sibling against."""
    if repo_root is None:
        return
    repo_root = Path(repo_root)

    offenders: list[str] = []
    for row in rows:
        candidates: list[str] = []
        writes = getattr(row, "writes", UNDECLARED)
        if writes is not UNDECLARED and isinstance(writes, list):
            candidates.extend(p for p in writes if isinstance(p, str) and p)
        candidates.extend(getattr(row, "writes_under", ()) or ())
        offenders.extend(
            f"{row.id} ({p!r})" for p in paths_outside_repo_root(candidates, repo_root)
        )

    if not offenders:
        return

    raise CrossRepoWriteError(
        f"writes: resolve outside repoRoot ({repo_root}) -- {'; '.join(offenders)}. "
        "The terminal commit is scoped to this worktree and can never land "
        "these paths; split into a per-repo plan and sequence with "
        "external_gate instead."
    )


def _gate_description(raw: Optional[Mapping]) -> str:
    gates = raw.get("external_gate") if isinstance(raw, Mapping) else None
    parts: list[str] = []
    for gate in gates if isinstance(gates, list) else ():
        if not isinstance(gate, Mapping):
            continue
        for key in ("owner_repo", "requires"):
            if gate.get(key):
                parts.append(f"{key}={gate[key]}")
    return " ".join(parts)


def gated_rows(
    exclusions: Sequence[Mapping], raw_by_id: Mapping[str, Mapping]
) -> list[GatedRow]:
    """Every ``external_gate`` and ``transitive_gate_closure`` exclusion as a
    ``GatedRow``, in exclusion order. Never raises.

    Negative-spec: other reasons are skipped, and exclusion ``detail``
    strings are copied through, not parsed."""
    out: list[GatedRow] = []
    for entry in exclusions:
        reason = entry.get("reason")
        row_id = entry.get("id")
        if reason == "external_gate":
            gate = _gate_description(raw_by_id.get(row_id)) or "external_gate"
        elif reason == "transitive_gate_closure":
            gate = str(entry.get("detail", ""))
        else:
            continue
        out.append(GatedRow(id=row_id, reason=reason, gate=gate))
    return out
