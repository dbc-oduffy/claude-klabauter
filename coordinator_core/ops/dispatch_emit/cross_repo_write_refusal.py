"""
coordinator_core.ops.dispatch_emit.cross_repo_write_refusal -- classify a
spine's declared ``writes:`` that land outside the emitting repo.

A path is outside ``repo_root`` when it is absolute (drive-letter or POSIX)
and not under ``repo_root``, when its normalised relative form escapes via
``..``, or when its first segment names a sibling git checkout.

Outside paths split two ways:

- **Sibling-repo writes** -- the path resolves under a git checkout sitting
  next to ``repo_root``. These are carried: ``dispatch.terminal_commit``
  lands one ``ceremony.commit_v2`` per repo (``split_sibling_paths``). On a
  remote (cloud) venue the run proceeds; elsewhere it halts on the
  ``approve_cross_repo_write`` touchpoint until the PM approves, and that
  approval is the run's cross-repo commit assent (``approved=True``).
- **Unreachable writes** -- outside every git checkout. No commit can land
  them, so the emit refuses (``CrossRepoWriteError``).

Trap: a sibling path must never fall through to ``terminal_commit``'s
home-repo commit -- it would be dropped as "absent and untracked at HEAD"
and sit uncommitted on disk. ``split_sibling_paths`` is the one resolver
both the emit check and the commit use; never re-derive it.

Negative-spec: this module does not walk the tree or call ``git status``.
It is pure path arithmetic plus one targeted ``.git``-presence probe on the
candidate a row itself named -- never a directory scan.

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
    """A declared write no commit can land (outside every git checkout)."""


class CrossRepoApprovalNeeded(CrossRepoWriteError):
    """Sibling-repo writes on a non-remote venue, not yet PM-approved."""


APPROVE_TOUCHPOINT = "approve_cross_repo_write"


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


def sibling_write(raw: str, repo_root: Path) -> Optional[tuple[Path, str]]:
    """``(sibling_worktree_root, repo-relative posix path)`` when ``raw``
    resolves under a git checkout that sits next to ``repo_root``; else None."""
    repo_root = Path(repo_root)
    posix = _posix(raw)
    if _is_absolute(posix):
        normalized = posixpath.normpath(posix)
        parent = _posix(str(repo_root.parent)).rstrip("/") + "/"
        if not normalized.startswith(parent):
            return None
        rest = normalized[len(parent):]
    else:
        normalized = posixpath.normpath(posix)
        rest = normalized[3:] if normalized.startswith("../") else normalized
        if rest.startswith("../"):
            return None
    segment = _first_segment(rest)
    if segment is None or "/" not in rest or not _is_sibling_repo_dir(repo_root, segment):
        return None
    return repo_root.parent / segment, rest.split("/", 1)[1]


def split_sibling_paths(
    paths: Iterable[str], repo_root: Path
) -> tuple[list[str], dict[Path, list[str]]]:
    """Partition ``paths`` into (home paths, {sibling root: its relative
    paths}), input order kept within each bucket."""
    home: list[str] = []
    siblings: dict[Path, list[str]] = {}
    for raw in paths:
        hit = sibling_write(raw, repo_root)
        if hit is None:
            home.append(raw)
        else:
            siblings.setdefault(hit[0], []).append(hit[1])
    return home, siblings


def is_remote_venue(env: Optional[Mapping[str, str]] = None) -> bool:
    """Cloud session per ``env_locality.harness_rung`` (``CLAUDE_CODE_REMOTE=true``)."""
    from coordinator_core.env_locality import harness_rung

    hit = harness_rung(env)
    return hit is not None and hit.call == "cloud"


def check_cross_repo_writes(
    rows,
    repo_root: Optional[Path],
    *,
    approved: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Classify every row's ``writes:`` / ``writes_under:`` outside
    ``repo_root``. Returns the sibling-repo names the run will write.

    Raises ``CrossRepoWriteError`` naming any write outside every checkout,
    and ``CrossRepoApprovalNeeded`` when sibling writes exist on a non-remote
    venue without ``approved``. No-op when ``repo_root`` is ``None``."""
    if repo_root is None:
        return []
    repo_root = Path(repo_root)

    unreachable: list[str] = []
    sibling_rows: list[str] = []
    repos: list[str] = []
    for row in rows:
        candidates: list[str] = []
        writes = getattr(row, "writes", UNDECLARED)
        if writes is not UNDECLARED and isinstance(writes, list):
            candidates.extend(p for p in writes if isinstance(p, str) and p)
        candidates.extend(getattr(row, "writes_under", ()) or ())
        for p in paths_outside_repo_root(candidates, repo_root):
            hit = sibling_write(p, repo_root)
            if hit is None:
                unreachable.append(f"{row.id} ({p!r})")
                continue
            sibling_rows.append(f"{row.id} ({p!r})")
            if hit[0].name not in repos:
                repos.append(hit[0].name)

    if unreachable:
        raise CrossRepoWriteError(
            f"writes: resolve outside every git checkout beside repoRoot ({repo_root}) -- "
            f"{'; '.join(unreachable)}. No commit can land these paths; name them "
            "under the owning repo's checkout."
        )
    if sibling_rows and not approved and not is_remote_venue(env):
        raise CrossRepoApprovalNeeded(
            f"{APPROVE_TOUCHPOINT}: this run writes into {', '.join(repos)} beside "
            f"{repo_root.name} -- {'; '.join(sibling_rows)}. Ask the PM whether this "
            "cross-repo write is okay; on approval re-emit with --cross-repo-approved "
            "(the approval is this session's cross-repo commit assent)."
        )
    return repos


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


def _first_gate_field(raw: Optional[Mapping], key: str):
    gates = raw.get("external_gate") if isinstance(raw, Mapping) else None
    for gate in gates if isinstance(gates, list) else ():
        if isinstance(gate, Mapping) and gate.get("cleared") is not True and gate.get(key):
            return gate[key]
    return None


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
        raw = raw_by_id.get(row_id)
        out.append(
            GatedRow(
                id=row_id, reason=reason, gate=gate,
                owner_repo=str(_first_gate_field(raw, "owner_repo") or ""),
                closure_key=_first_gate_field(raw, "closure_key"),
            )
        )
    return out
