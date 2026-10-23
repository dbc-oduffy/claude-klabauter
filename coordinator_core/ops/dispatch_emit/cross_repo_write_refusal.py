"""
coordinator_core.ops.dispatch_emit.cross_repo_write_refusal -- classify a
spine's declared ``writes:`` that land outside the emitting repo.

A path is outside ``repo_root`` when it is absolute (drive-letter or POSIX)
and not under ``repo_root``, when its normalised relative form escapes via
``..``, or when its first segment names a sibling git checkout.

Outside paths split two ways:

- **Sibling-repo writes** -- the path resolves under a git checkout sitting
  next to ``repo_root``. These are carried: ``dispatch.land_run``
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

import os
import posixpath
import re
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.ask_contract import GatedRow
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.sibling_repo_provision_contract import (
    ROUTE_REMEDY,
    provisioned_local_root,
    provisioned_target_of,
)

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


def in_session_scratchpad(posix_path: str) -> bool:
    """True for an absolute path under ``<tempdir>/claude/<project-slug>/<session>/scratchpad/``.
    Any session's scratchpad counts; the rest of the temp dir does not."""
    candidate = posixpath.normpath(posix_path)
    tempdir = Path(tempfile.gettempdir())
    # Both spellings: a harness-issued path may be the symlinked form (macOS /var vs /private/var)
    # or the 8.3 short form (Windows) that resolve() rewrites.
    for temp in {_posix(str(tempdir)), _posix(str(tempdir.resolve()))}:
        if not _under_root(candidate, temp):
            continue
        parts = candidate[len(posixpath.normpath(temp).rstrip("/")):].lstrip("/").split("/")
        if len(parts) >= 5 and parts[0].casefold() == "claude" and parts[3] == "scratchpad":
            return True
    return False


def paths_outside_repo_root(paths: Iterable[str], repo_root: Path) -> list[str]:
    """The paths a terminal commit keyed on ``repo_root`` cannot reach, in
    input order: absolute and not under ``repo_root`` or a session scratchpad,
    ``..``-escaping, or first-segment-sibling-repo."""
    root_posix = _posix(str(repo_root))
    repo_root = Path(repo_root)
    out: list[str] = []
    for raw in paths:
        posix = _posix(raw)
        if _is_absolute(posix):
            if not _under_root(posix, root_posix) and not in_session_scratchpad(posix):
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


GRANT_FILE = "cross-repo-writes.txt"


def grant_file(repo_root: Path, session_id: str) -> Path:
    """The run's cross-repo grant ledger: one absolute posix sibling path per line, written when
    a run's sibling ``writes:`` pass the approval check and read by
    ``write_guards.block_cross_repo_agent_write``."""
    return Path(repo_root) / ".git" / "coordinator-sessions" / session_id / GRANT_FILE


def record_grants(repo_root: Path, session_id: str, sibling_paths: Iterable[str]) -> None:
    """Append ``sibling_paths`` (absolute) to the session's grant ledger, skipping lines already
    present. Best-effort: an unwritable ledger leaves the guard denying, never raising."""
    wanted = [_posix(p) for p in sibling_paths]
    if not wanted:
        return
    path = grant_file(repo_root, session_id)
    try:
        have = set(path.read_text(encoding="utf-8").split("\n")) if path.is_file() else set()
        fresh = [p for p in dict.fromkeys(wanted) if p not in have]
        if fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(fresh) + "\n")
    except OSError:
        return


def _absent_sibling_name(raw: str, repo_root: Path) -> Optional[str]:
    """The first segment of ``raw`` under ``repo_root``'s parent when that child does not exist
    yet (a sibling a provision op could create); else None."""
    posix = _posix(raw)
    if _is_absolute(posix):
        parent = _posix(str(repo_root.parent)).rstrip("/") + "/"
        normalized = posixpath.normpath(posix)
        if not normalized.startswith(parent):
            return None
        rest = normalized[len(parent):]
    else:
        normalized = posixpath.normpath(posix)
        if not normalized.startswith("../"):
            return None
        rest = normalized[3:]
        if rest.startswith("../"):
            return None
    segment = _first_segment(rest)
    if segment is None or "/" not in rest or segment in (".", ".."):
        return None
    return None if (repo_root.parent / segment).exists() else segment


def check_cross_repo_writes(
    rows,
    repo_root: Optional[Path],
    *,
    approved: bool = False,
    env: Optional[Mapping[str, str]] = None,
    session_id: Optional[str] = None,
) -> list[str]:
    """Classify every row's ``writes:`` / ``writes_under:`` outside
    ``repo_root``. Returns the sibling-repo names the run will write.

    Raises ``CrossRepoWriteError`` naming any write outside every checkout,
    and ``CrossRepoApprovalNeeded`` when sibling writes exist on a non-remote
    venue without ``approved``. No-op when ``repo_root`` is ``None``. With
    ``session_id``, the sibling paths of a passing check are recorded as the
    run's cross-repo grant (``record_grants``)."""
    if repo_root is None:
        return []
    repo_root = Path(repo_root)

    unreachable: list[str] = []
    absent_sibling: list[str] = []
    sibling_rows: list[str] = []
    granted: list[str] = []
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
                name = _absent_sibling_name(p, repo_root)
                if name and name not in absent_sibling:
                    absent_sibling.append(name)
                continue
            if provisioned_target_of(hit[0] / hit[1], repo_root) is not None:
                raise CrossRepoWriteError(
                    f"writes: {row.id} ({p!r}) lands inside the provisioned local sibling "
                    f"{hit[0].name}, a publish target. {ROUTE_REMEDY}"
                )
            sibling_rows.append(f"{row.id} ({p!r})")
            granted.append(_posix(str(hit[0] / hit[1])))
            if hit[0].name not in repos:
                repos.append(hit[0].name)

    if unreachable:
        hint = f" {ROUTE_REMEDY}" if absent_sibling else ""
        raise CrossRepoWriteError(
            f"writes: resolve outside every git checkout beside repoRoot ({repo_root}) -- "
            f"{'; '.join(unreachable)}. No commit can land these paths; name them "
            f"under the owning repo's checkout.{hint}"
        )
    if sibling_rows and not approved and not is_remote_venue(env):
        raise CrossRepoApprovalNeeded(
            f"{APPROVE_TOUCHPOINT}: this run writes into {', '.join(repos)} beside "
            f"{repo_root.name} -- {'; '.join(sibling_rows)}. Ask the PM whether this "
            "cross-repo write is okay; on approval re-emit with --cross-repo-approved "
            "(the approval is this session's cross-repo commit assent)."
        )
    if session_id and granted:
        record_grants(repo_root, session_id, granted)
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
    strings are copied through, not parsed. A transitive row reads ``owner_repo`` and
    ``closure_key`` from its root row's gate (the entry's ``root`` key)."""
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
        gate_row = entry.get("root") if reason == "transitive_gate_closure" and entry.get("root") else row_id
        raw = raw_by_id.get(gate_row)
        own = raw_by_id.get(row_id)
        regs = own.get("register_rows") if isinstance(own, Mapping) else None
        out.append(
            GatedRow(
                id=row_id, reason=reason, gate=gate,
                owner_repo=str(_first_gate_field(raw, "owner_repo") or ""),
                closure_key=_first_gate_field(raw, "closure_key"),
                title=str(own.get("title") or "")[:200] if isinstance(own, Mapping) else "",
                register_rows=tuple(str(r) for r in regs) if isinstance(regs, list) else (),
            )
        )
    return out


def slot_withheld_rows(raw_by_id: Mapping[str, Mapping]) -> list[dict]:
    """``{id, title, register_rows}`` of each open ``needs_slot`` row: its process leg waits on a
    slot, so the clauses it delivers are withheld from the run's judge like a gated row's."""
    from coordinator_core.ops.dispatch_emit.spine_read import NON_DISPATCHABLE_DISPOSITIONS

    out: list[dict] = []
    for rid, raw in raw_by_id.items():
        if not isinstance(raw, Mapping) or raw.get("needs_slot") is not True:
            continue
        if raw.get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
            continue
        regs = raw.get("register_rows")
        out.append({
            "id": str(rid), "title": str(raw.get("title") or "")[:200],
            "register_rows": [str(r) for r in regs] if isinstance(regs, list) else [],
        })
    return out


def _same_root(a: str, b: object) -> bool:
    if not a:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(str(b)))


def own_gate_owners(raw_rows: Iterable[Mapping], root: Path) -> frozenset[str]:
    """The ``owner_repo`` names in ``raw_rows``' ``external_gate`` entries whose registered root is
    ``root``: the run's own repo, so a gate naming it withholds nothing. A third repo's gate, and an
    unregistered owner, are not in the set. No process is spawned."""
    owners = {
        g.get("owner_repo")
        for raw in raw_rows
        if isinstance(raw, Mapping)
        for g in (raw.get("external_gate") if isinstance(raw.get("external_gate"), list) else ())
        if isinstance(g, Mapping) and isinstance(g.get("owner_repo"), str) and g["owner_repo"]
    }
    if not owners:
        return frozenset()
    from coordinator_core.machine_resolver import registry_get
    from coordinator_core.ops.fleet._memo_resolver import receiver_em_to_repo_key

    return frozenset(o for o in owners if _same_root(registry_get(receiver_em_to_repo_key(o)) or "", root))


def provisioned_sibling_roots(raw_rows: Iterable[Mapping], root: Path) -> dict[str, Path]:
    """``{owner_repo: root}`` for owners whose every uncleared ``external_gate`` is
    ``requires: commit-in-owner-repo`` and whose registered root is a marked, remote-free sibling
    of ``root``. An owner with any other uncleared gate (``landed-work``) is absent. No process
    is spawned."""
    owners: dict[str, bool] = {}
    for raw in raw_rows:
        gates = raw.get("external_gate") if isinstance(raw, Mapping) else None
        for g in gates if isinstance(gates, list) else ():
            if not isinstance(g, Mapping) or g.get("cleared") is True:
                continue
            owner = g.get("owner_repo")
            if isinstance(owner, str) and owner:
                owners[owner] = owners.get(owner, True) and g.get("requires") == "commit-in-owner-repo"
    out: dict[str, Path] = {}
    for owner in (o for o, ok in owners.items() if ok):
        hit = provisioned_local_root(owner)
        if hit is not None and _same_root(str(hit.parent), Path(root).parent) and not _same_root(str(hit), root):
            out[owner] = hit
    return out

