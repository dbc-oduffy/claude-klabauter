"""
coordinator_core.ops.reap_orphaned_agent_dirs — mechanical, rail-gated reaper
for orphaned per-agent bookkeeping dirs under
``.git/coordinator-sessions/.agents/<canonical-agent-id>/``.

Purpose: named-teammate-shaped agent dirs (``<name>@session-<short>``) whose
owning EM session directory has been removed (SessionEnd archival/removal
takes the session's ``dispatched-agents.txt`` with it, but ``.agents/`` dirs
persist forever — this is survivorship, not a lost write; see
state/audits/2026-08-14-orphan-agent-dir-reap.md for the census that
established this). This module is the DURABLE, re-runnable answer: a
deterministic classifier + deleter, not a one-off manual pass.

Four safety rails — a dir is an archival CANDIDATE only if ALL hold (fail
closed on any ambiguity — unknown ownership is NOT candidate):
  R1 liveness  : the owning session id (``em-session-id.txt`` contents) is
                 NOT live, via ``coordinator_core.session.liveness.session_live``.
                 Absent/unreadable ``em-session-id.txt`` => NOT a candidate
                 (unknown ownership fails closed) — see ``_classify``: this
                 is unconditional. R2 cannot be evaluated without a readable
                 owner id in the first place (``session_dir_exists`` is only
                 ever set to non-``None`` inside the ``owner_id`` branch), so
                 there is no code path where R2 "independently clears" a
                 missing/unreadable owner id — the missing-owner-id case is
                 fully fail-closed, no exception.
  R2 sessiondir: ``.git/coordinator-sessions/<owner-id>/`` does not exist.
  R3 clean     : no path named in ``touched.txt`` is currently dirty in the
                 calling repo's working tree (``git status --porcelain``) —
                 this claude-klabauter repo IS the working tree for this reaper's use
                 case (agent dirs under this repo's own coordinator-sessions
                 substrate); a touched path outside this repo cannot be dirty
                 here and does not block on that basis. Comparison is prefix-
                 aware (a touched.txt entry naming a directory also matches
                 file-level dirt reported beneath it) and case-insensitive
                 (this repo treats Windows, case-insensitive, as first-class).
  R4 age       : dir mtime is older than 24h.

Negative-spec:
  - Does NOT touch the existing session.reap sub-reap (ii) (_reap_stale_agents
    in coordinator_core/ops/session/reap.py) — that mechanism ARCHIVES
    (mv to .archive/) purely on touched.txt mtime>24h, with no liveness or
    dirty-file check; it is a different (looser) safety contract and stays
    unchanged. This module archives the stricter, liveness-gated subset,
    reusing the exact same ``.archive/_agents-<agent_id>-<YYYYMMDD>/`` shape
    (and its 14-day ``_prune_stale_agent_archive`` sweep) — EM decision
    2026-08-14: an archive-then-later-prune shape lowers the cost of a wrong
    classification (a rollback window), not just its probability, at near-zero
    additional cost; a plain ``rmtree`` here would be the only rail in this
    module's four-rail chain with no recovery path if any rail had a latent
    bug.
  - Does NOT wire itself into SessionEnd/session-archival by default — see
    ``main``'s --apply path and the module's audit-file "Durable fix" note
    for why (cross-repo wiring cost vs. this repo's own boundary).
  - Does NOT archive anything on a dir failing ANY rail — partial rail
    clearance is not clearance.
  - Does NOT guard against two concurrent invocations of this reaper itself
    (e.g. an operator re-running --apply twice, or a future SessionEnd-wired
    caller racing a manual run) double-archiving the same candidate set. This
    is benign today: the archive-collision path below (mirroring
    ``_reap_stale_agents``) treats an already-existing archive dest as
    idempotent-skip, and a genuine ``OSError`` is caught into ``failed[]``,
    never silent data loss — surfaced here for awareness, not fixed by a
    lock, since the current failure mode is a caught exception, not data loss.

CLI:
  python -m coordinator_core.ops.reap_orphaned_agent_dirs --dry-run [--audit PATH]
  python -m coordinator_core.ops.reap_orphaned_agent_dirs --apply [--audit PATH]

--dry-run (default if neither flag given): classifies every dir, writes the
full audit report (candidates + skip reasons + totals) to --audit (default
state/audits/2026-08-14-orphan-agent-dir-reap.md), archives nothing.
--apply: same classification, writes the audit, then archives exactly the
candidate set to ``.archive/_agents-<agent_id>-<YYYYMMDD>/`` (same shape as
session.reap sub-reap (ii); see ``_prune_stale_agent_archive`` for the 14-day
retention sweep that later prunes these). Idempotent — a second run against
an already-clean tree enumerates 0 candidates and is a no-op.

Audit-file overwrite safety: BOTH --dry-run and --apply write a
mode-and-UTC-timestamped audit file (``<stem>.<mode>-<stamp><suffix>``) when
--audit is left at its default — a truncated audit is the only record of
which dirs were touched, and the dirs themselves may already be gone, so
neither mode is allowed to silently overwrite another run's record. An
explicit --audit PATH is honoured verbatim (no timestamp inserted).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.session.reap import _today_compact
from coordinator_core.session.liveness import session_live

_AGE_THRESHOLD_SECONDS = 24 * 3600
_DEFAULT_AUDIT_PATH = "state/audits/2026-08-14-orphan-agent-dir-reap.md"


@dataclass
class Verdict:
    agent_dir: Path
    candidate: bool
    reason: str
    owner_id: Optional[str] = None
    touched_paths: List[str] = field(default_factory=list)


def _read_owner_id(agent_dir: Path) -> Optional[str]:
    owner_file = agent_dir / "em-session-id.txt"
    try:
        text = owner_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _read_touched_paths(agent_dir: Path) -> List[str]:
    touched_file = agent_dir / "touched.txt"
    try:
        text = touched_file.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _dirty_paths(repo_root: Path) -> set:
    """Return the set of paths (repo-relative, forward-slash) with any
    working-tree dirt (`git status --porcelain`), or a sentinel treated as
    "everything dirty" (fail-closed) on any git-invocation failure.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError):
        return {"*"}  # fail-closed: treat every touched path as dirty
    paths = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:  # rename: "old -> new"
            old, new = entry.split(" -> ", 1)
            paths.add(old.strip().strip('"'))
            paths.add(new.strip().strip('"'))
        else:
            paths.add(entry.strip().strip('"'))
    return paths


def _touched_path_is_dirty(touched_paths: List[str], dirty: set) -> Optional[str]:
    # Review: code-reviewer R3 finding (2026-08-14 slice2) — exact string
    # equality failed open on two shapes: (a) a touched.txt entry naming a
    # directory while git reports dirt at file-level paths beneath it, and
    # (b) a case difference between the two sources on this repo's
    # first-class-Windows, case-insensitive filesystem. Comparison is now
    # prefix-aware (a touched dir also matches any dirty entry beneath it)
    # and case-insensitive.
    if "*" in dirty:
        return "git status --porcelain failed — fail-closed, treating as dirty"
    dirty_lower = {d.lower() for d in dirty}
    for p in touched_paths:
        norm = p.replace("\\", "/")
        norm_lower = norm.lower()
        if norm_lower in dirty_lower:
            return f"touched path still dirty in working tree: {p}"
        prefix = norm_lower.rstrip("/") + "/"
        if any(d.startswith(prefix) for d in dirty_lower):
            return f"touched path still dirty in working tree (directory prefix match): {p}"
    return None


def _classify(
    agent_dir: Path,
    sessions_dir: Path,
    dirty: set,
    now: float,
    repo_root: Path,
) -> Verdict:
    owner_id = _read_owner_id(agent_dir)
    touched_paths = _read_touched_paths(agent_dir)

    session_dir_exists: Optional[bool] = None
    if owner_id:
        session_dir_exists = (sessions_dir / owner_id).exists()

    # R1: liveness. Unknown ownership fails closed, unconditionally — R2 can
    # only be evaluated when owner_id is known (session_dir_exists is set
    # below only inside `if owner_id:`), so there is no branch here for R2 to
    # "independently clear" a missing/unreadable owner id.
    if not owner_id:
        if session_dir_exists is None:
            # No owner id at all — not a candidate (unknown ownership, R1
            # fails, nothing clears it).
            return Verdict(agent_dir, False, "em-session-id.txt missing/unreadable — unknown ownership, R1 fails", owner_id, touched_paths)

    if owner_id:
        try:
            is_live = session_live(owner_id, cwd=str(repo_root))
        except Exception as exc:  # fail-closed on any liveness-check error
            return Verdict(agent_dir, False, f"session_live raised for owner {owner_id!r} — fail-closed: {exc}", owner_id, touched_paths)
        if is_live:
            return Verdict(agent_dir, False, f"owning session {owner_id} is LIVE — R1 fails", owner_id, touched_paths)

    # R2: owning session directory must not exist.
    if owner_id:
        if session_dir_exists:
            return Verdict(agent_dir, False, f"owning session dir still exists: {sessions_dir / owner_id}", owner_id, touched_paths)
    else:
        # Unreachable: the `if not owner_id: return ...` above already
        # returns whenever owner_id is falsy, so this arm never executes.
        # Kept only as a defensive fallback matching the R1 branch's verdict.
        return Verdict(agent_dir, False, "em-session-id.txt missing/unreadable — unknown ownership", owner_id, touched_paths)

    # R3: nothing in touched.txt currently dirty.
    dirty_reason = _touched_path_is_dirty(touched_paths, dirty)
    if dirty_reason:
        return Verdict(agent_dir, False, dirty_reason, owner_id, touched_paths)

    # R4: mtime older than 24h.
    try:
        mtime = agent_dir.stat().st_mtime
    except OSError as exc:
        return Verdict(agent_dir, False, f"stat failed — fail-closed: {exc}", owner_id, touched_paths)
    age = now - mtime
    if age < 0:
        age = 0.0
    if age <= _AGE_THRESHOLD_SECONDS:
        return Verdict(agent_dir, False, f"mtime within 24h (age={age:.0f}s) — R4 fails", owner_id, touched_paths)

    return Verdict(
        agent_dir,
        True,
        f"cleared all 4 rails: owner {owner_id} not live, session dir absent, "
        f"no dirty touched paths, age={age:.0f}s",
        owner_id,
        touched_paths,
    )


def _classify_all(repo_root: Path) -> List[Verdict]:
    common_dir = git_common_dir(repo_root)
    sessions_dir = common_dir / "coordinator-sessions"
    agents_base = sessions_dir / ".agents"
    if not agents_base.is_dir():
        return []

    dirty = _dirty_paths(repo_root)
    now = time.time()

    verdicts: List[Verdict] = []
    for adir in sorted(agents_base.iterdir()):
        if not adir.is_dir() or adir.name.startswith("."):
            continue
        verdicts.append(_classify(adir, sessions_dir, dirty, now, repo_root))
    return verdicts


def _archive_candidate(agent_dir: Path, sessions_dir: Path) -> Optional[str]:
    """Archive one candidate to ``.archive/_agents-<agent_id>-<YYYYMMDD>/`` —
    same shape and collision handling as session.reap sub-reap (ii)'s
    ``_reap_stale_agents`` (coordinator_core/ops/session/reap.py), reused
    rather than reinvented so the two archival paths stay one shape. Returns
    None on success, or an error string on failure.
    """
    archive_root = sessions_dir / ".archive"
    archive_dest = archive_root / f"_agents-{agent_dir.name}-{_today_compact()}"

    if archive_dest.exists():
        # Already archived (idempotent — mirrors _reap_stale_agents).
        return None

    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        agent_dir.rename(archive_dest)
        return None
    except OSError as exc:
        if archive_dest.exists():
            # Concurrent reap already moved it — idempotent.
            return None
        return str(exc)


def _write_audit(audit_path: Path, verdicts: List[Verdict], applied: bool) -> None:
    total = len(verdicts)
    candidates = [v for v in verdicts if v.candidate]
    skipped = [v for v in verdicts if not v.candidate]

    lines: List[str] = []
    lines.append("# Orphaned agent-dir reap — audit\n")
    lines.append(f"Mode: {'APPLY (archived)' if applied else 'DRY-RUN (no changes)'}\n")
    lines.append("")
    lines.append(f"- Total `.agents/` dirs scanned: {total}")
    lines.append(f"- Candidates (cleared all 4 rails): {len(candidates)}")
    lines.append(f"- Skipped (failed at least one rail): {len(skipped)}")
    lines.append("")
    lines.append("## Candidates")
    lines.append("")
    if candidates:
        for v in candidates:
            lines.append(f"- `{v.agent_dir.name}` — {v.reason}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Skipped")
    lines.append("")
    if skipped:
        for v in skipped:
            lines.append(f"- `{v.agent_dir.name}` — {v.reason}")
    else:
        lines.append("(none)")
    lines.append("")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="classify + write audit only (default)")
    mode.add_argument("--apply", action="store_true", help="classify, write audit, then archive candidates to .archive/_agents-<agent_id>-<YYYYMMDD>/")
    parser.add_argument("--repo-root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--audit", default=_DEFAULT_AUDIT_PATH, help="audit file path (repo-relative or absolute)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    audit_path = Path(args.audit)
    if not audit_path.is_absolute():
        audit_path = repo_root / audit_path

    # An apply run's audit is the only record of WHICH dirs were archived, and
    # a dry-run's is the only record of what it WOULD have touched -- either
    # mode truncating the other's (or its own prior run's) record at the
    # stable default path destroys unrecoverable evidence (observed once,
    # 2026-08-14, on the apply branch; the dry-run branch had the identical
    # gap open). Both modes therefore get their own mode-and-UTC-timestamped
    # file when --audit is left at its default; an explicit --audit PATH is
    # honoured verbatim (no timestamp inserted).
    if args.audit == _DEFAULT_AUDIT_PATH:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        mode_tag = "apply" if args.apply else "dry-run"
        audit_path = audit_path.with_name(f"{audit_path.stem}.{mode_tag}-{stamp}{audit_path.suffix}")

    verdicts = _classify_all(repo_root)
    candidates = [v for v in verdicts if v.candidate]

    applied = bool(args.apply)
    archived: List[str] = []
    failed: List[str] = []
    if applied:
        common_dir = git_common_dir(repo_root)
        sessions_dir = common_dir / "coordinator-sessions"
        for v in candidates:
            err = _archive_candidate(v.agent_dir, sessions_dir)
            if err is None:
                archived.append(v.agent_dir.name)
            else:
                failed.append(f"{v.agent_dir.name}: {err}")

    _write_audit(audit_path, verdicts, applied)

    total = len(verdicts)
    print(f"scanned={total} candidates={len(candidates)} skipped={total - len(candidates)}")
    if applied:
        print(f"archived={len(archived)} failed={len(failed)}")
        for f in failed:
            print(f"  FAILED: {f}", file=sys.stderr)
    print(f"audit written to {audit_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
