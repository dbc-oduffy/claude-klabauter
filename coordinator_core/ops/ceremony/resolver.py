"""
coordinator_core.ops.ceremony.resolver — public resolver helpers shared by
branch_resolution.py (formerly wsc_resolve.py) and the wsc_tail rebuild.

Purpose: promotes three previously module-private helpers to a shared PUBLIC
contract, so a future refactor of branch_resolution.py's internals cannot
silently break the wsc_tail rebuild's consumed-handoff stamping (C5) with no
compile-time signal. Before this module existed, wsc_tail would have had to
reach across a module boundary at ``coordinator_core.ops.ceremony.wsc_resolve
._resolve_in_repo`` / ``._find_all_consumed_handoffs`` — a private-import
reach this module exists to close.

``branch_resolution.py`` imports ``resolve_in_repo`` / ``find_all_consumed_handoffs``
from here (aliased back to its historical ``_resolve_in_repo`` /
``_find_all_consumed_handoffs`` names so every one of its existing internal
call sites is unchanged) instead of defining them locally.
``get_handoff_consumed_by`` re-exports ``coverage._get_handoff_consumed_by``
unchanged under a public name — that function's implementation stays in
``coverage.py`` (it is used broadly outside this rebuild's scope, e.g.
``coordinator_core/ops/fleet/archive_handoffs.py``; relocating its body is
out of this chunk's scope).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C5
("First promote the shared resolver helpers ... to a public
coordinator_core/ops/ceremony/resolver.py ... that both wsc_resolve and this
tail import from — reuse via public contract, not private-import reach
across a module boundary").

Negative-spec (hard-won):
  - Does NOT duplicate ``_get_handoff_consumed_by``'s frontmatter-scan logic
    — re-exports the single canonical implementation from ``coverage.py`` so
    the resolver and the C2 idempotency guard never disagree on what
    "consumed_by" means (branch_resolution.py's own docstring already
    documents this "same definition" invariant; a duplicate here would
    silently reintroduce the drift risk that invariant exists to prevent).
  - Does NOT import ``branch_resolution.py`` (formerly ``wsc_resolve.py``) —
    that would be circular (branch_resolution.py imports this module).
    ``find_all_consumed_handoffs`` therefore carries its own minimal
    frontmatter parser, deliberately NOT branch_resolution.py's
    ``_parse_frontmatter`` (which stays module-private there — it has five
    other call sites unrelated to this promotion and relocating it is out of
    this chunk's scope). This bar is why the 2026-07-29 kill-list extraction
    (moving _read_session_shape/_resolve_branches/_session_commit_log out of
    the retired wsc_resolve.py) did NOT land here — importing them into a
    module branch_resolution.py itself must never import from would recreate
    exactly the circularity this negative-spec forbids.
  - Does NOT change matching/ordering semantics from the pre-promotion
    implementations — this is a pure relocation, not a redesign.
"""

from __future__ import annotations
import sys

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.coverage import _get_handoff_consumed_by as get_handoff_consumed_by
from coordinator_core.ops.fleet._common import rel_id
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)

__all__ = [
    "resolve_in_repo",
    "find_all_consumed_handoffs",
    "get_handoff_consumed_by",
    "detect_git_provenance_consumed",
]


def resolve_in_repo(worktree_root: Path, candidate_path: str) -> Path | None:
    """Return the absolute path for candidate_path IFF it resolves to a location
    contained within worktree_root; else None.

    Guards against a producer-written pickup.handoff that is absolute or
    uses ../ traversal to escape the invoking repo — the foreign-repo bleed
    defect (a example-retrieval-repo handoff mis-bound into a DoE ceremony).  Containment,
    not mere existence, is the invariant: hf must live under THIS repo.

    Negative-spec: do NOT downgrade to a string-prefix check — resolve() first;
    an absolute or ../ pickup.handoff value is the foreign-repo bleed vector.  A naive
    ``str(cand).startswith(str(root))`` check is defeated by both an absolute
    path pointing at a sibling repo whose name happens to share a prefix
    (e.g. worktree_root=".../claude-klabauter" vs candidate="/.../claude_klabauter2/x")
    and by un-normalized ``..`` segments that a prefix check never walks.

    Existence of the returned path is NOT checked here — that remains the
    caller's responsibility (this function only answers "is it contained").
    """
    root = worktree_root.resolve()
    cand = (worktree_root / candidate_path).resolve()
    if cand == root or root in cand.parents:
        return cand
    return None


def _parse_frontmatter_flat(text: str) -> dict[str, Any]:
    """Extract YAML-like key: value pairs from a markdown frontmatter block.

    Minimal parser — extracts only the --- ... --- block and returns a flat dict
    of {key: value_string}.  Does NOT parse nested YAML; not a full YAML parser.
    This is sufficient for reading status, consumed_by, chain, etc.

    Deliberate duplicate of branch_resolution.py's ``_parse_frontmatter`` — see
    this module's negative-spec for why it is not imported instead (circular import).

    Keys are anchored at column 0, matching the bash oracle's
    ``grep -qE "^predecessor:"`` — an indented ``key: value`` (e.g. nested under
    a parent key) is NOT matched.  A lenient any-indentation match would be an
    unintended divergence from the oracle this parser ports.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith((" ", "\t")):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm


def find_all_consumed_handoffs(
    worktree_root: Path,
    sid: str,
    *,
    scan_errors: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return EVERY (handoff_path, frontmatter) pair consumed by sid.

    Scans BOTH state/handoffs/*.md (live) AND archive/handoffs/**/*.md (swept)
    for a frontmatter ``consumed_by: <sid>`` match, so a predecessor handoff
    already archived by fleet.archive_completed_handoffs is still found.
    Collects EVERY match (not the first) — a single session can DAG-pick-up N
    handoffs at once. Returns [] when none found.

    Uses the anchored frontmatter check from ``get_handoff_consumed_by``
    (re-exported from ``coordinator_core.coverage`` — the SAME definition the
    C2 idempotency guard uses) instead of a naive substring test, so resolver
    and guard agree on one definition of consumed_by and body-prose mentions
    of a sibling session-id (e.g. "see also sess-xyz") do not false-positive-match.

    Negative-spec: do NOT reintroduce ``f"consumed_by: {sid}" in text`` — that
    substring test matches ANYWHERE in the handoff body, not just frontmatter.

    Scale note: this is an unindexed O(files) linear scan over state/handoffs/
    and archive/handoffs/ (the latter grows unbounded — it is the sweep
    destination for all completed handoffs across the repo's lifetime).
    Candidates are sorted reverse-lexical by full path string — an
    approximation of newest-first valid only for date-prefixed archive
    subfolders (e.g. archive/handoffs/2026-07/); this is NOT a temporal
    guarantee across the live/archive boundary. Does not affect correctness
    of the returned SET.

    scan_errors: optional out-param the caller owns.  When supplied, one
    human-readable string is appended per unreadable subtree encountered
    while walking either candidate dir — an enumeration gap under
    state/handoffs/ or archive/handoffs/ is NOT the same as "sid consumed no
    predecessor handoff" and must not be silently indistinguishable from a
    genuine empty result (a scan gap here can otherwise leave
    WSC_DISPOSITION defaulting to "not a chain continuation" with no visible
    signal that the scan was incomplete). Callers that omit this parameter
    get the pre-existing best-effort contract of this function unchanged
    (matches list only).

    Uses os.walk(onerror=...) per candidate dir, NOT ``.rglob("*.md")`` —
    Path.rglob's selector silently swallows PermissionError while walking
    (verified: an unreadable subtree yields nothing under it, no exception
    raised), which previously made an unreadable predecessor-handoff subtree
    indistinguishable from "no predecessor handoff exists here".
    """
    candidate_dirs = [
        worktree_root / "state" / "handoffs",
        worktree_root / "archive" / "handoffs",
    ]

    candidates: list[Path] = []
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        walk_errors: list[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(d, onerror=walk_errors.append):
            for fn in filenames:
                if fn.endswith(".md"):
                    candidates.append(Path(dirpath) / fn)
        if walk_errors:
            for exc in walk_errors:
                _LOG.warning(
                    "find_all_consumed_handoffs: cannot scan %s — %s; subtree "
                    "dropped (a predecessor handoff under it would be "
                    "invisible to this scan)",
                    getattr(exc, "filename", d),
                    exc,
                )
                if scan_errors is not None:
                    scan_errors.append(f"{getattr(exc, 'filename', d)}: {exc}")
    candidates.sort(reverse=True)

    matches: list[tuple[str, dict[str, Any]]] = []
    for hf in candidates:
        consumed_by = get_handoff_consumed_by(str(hf))
        if consumed_by == sid:
            try:
                text = hf.read_text(encoding="utf-8")
            except OSError:
                print(f"skip: find_all_consumed_handoffs: text = hf.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            fm = _parse_frontmatter_flat(text)
            matches.append((rel_id(hf, worktree_root), fm))
    return matches


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git subcommand, suppressing exceptions; return the CompletedProcess.

    A local sibling of branch_resolution.py's ``_git_run`` — not imported from
    there to avoid the circular-import this module's docstring already forbids
    (see negative-spec: "does NOT import branch_resolution.py").
    """
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **no_console_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _LOG.warning("detect_git_provenance_consumed: git %s failed: %s", args[0] if args else "?", exc)
        proc = subprocess.CompletedProcess(args=["git"] + args, returncode=2)
        proc.stdout = ""
        proc.stderr = str(exc)
        return proc


def detect_git_provenance_consumed(
    worktree_root: Path,
    sid: str,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Detector B — git-provenance chain-terminal detection (ported from the
    bash /workstream-complete SKILL.md Step 0 oracle).

    Catches the ship-then-archive shape ``find_all_consumed_handoffs`` (the
    live-scan / archive-scan "Detector A") cannot see: a session that
    archived a predecessor handoff THIS run with no live ``consumed_by:``
    stamp ever written (awaiting_gate/direct ship, unconsume/repark churn,
    sid skew, or an async sweep archiving between consume and WSC). Detector
    B instead reads git's OWN provenance — did a commit THIS session authored
    (``Session-Id:`` trailer == sid) Add/Rename/Copy an ``archive/handoffs/*.md``
    file? — sidestepping the live-stamp dependency entirely.

    Scans ``git log $(git merge-base origin/main HEAD)..HEAD --diff-filter=ARC
    --find-renames --name-status`` with a per-commit ``%(trailers:key=Session-
    Id,valueonly)`` marker line, mirroring the bash oracle's own
    ``awk -v sid=`` pass line-for-line (see
    ``DoE-claude/coordinator/skills/workstream-complete/SKILL.md`` Step 0,
    Detector B). Collects EVERY match across the range (not just the first —
    unlike the bash oracle's awk ``exit`` at first hit — so a DAG-pickup
    session that shipped/archived N predecessor handoffs this run gets all N
    considered, each independently gated below) rather than reproducing the
    bash single-hit shortcut.

    Two guards apply to every raw git-provenance candidate before it becomes
    an accepted hit, checked in the oracle's own order — spoof FIRST,
    well-formedness inside its else-branch — so a candidate that is BOTH
    malformed AND foreign-consumed surfaces the spoof diagnostic (names the
    real consumer) rather than the generic "no predecessor:" one:

      1. **Foreign-consumer / restoration-commit spoof guard** (2026-07-22
         incident: a restoration commit re-ADDs a handoff actually consumed
         by a DIFFERENT session, carrying THIS session's trailer and matching
         ``--diff-filter=A`` — naively attributing it to sid would be wrong).
         When the candidate's own ``consumed_by:`` frontmatter is non-empty
         AND names a session other than sid, the hit is REJECTED and a
         diagnostic names the real consumer. An empty/absent ``consumed_by:``
         passes through (the guard's job is to catch a *foreign* stamp, not
         to require one — an unconsumed archived handoff is exactly Detector
         B's whole reason to exist) to the well-formedness guard below.
      2. **Well-formedness guard** — only reached when the spoof guard passed
         through; the candidate file must carry a ``^predecessor:``
         frontmatter line (even ``null``, for a root/spinoff handoff) to
         bind; a malformed non-handoff file that merely lives under
         ``archive/handoffs/`` is rejected with a diagnostic.

    Returns (hits, warnings):
      hits     — accepted (path, frontmatter) pairs, deduped by path, each
                 already frontmatter-parsed for the caller's downstream use
                 (predecessor/consumed_by, same shape as
                 ``find_all_consumed_handoffs``'s return).
      warnings — one human-readable string per: an unresolvable
                 ``origin/main`` merge-base (degrade-loud, not silent-skip —
                 chain-terminal could be missed), a git-log failure, or a
                 candidate rejected by either guard above. Callers MUST fold
                 these into their evidence/diagnostics structure — a hit-list
                 this function could not fully compute is never the same as
                 "this session shipped/archived nothing."

    Negative-spec: does NOT crash on a missing origin/main (no such remote,
    detached/shallow clone, or a fresh repo with no push yet) — that is the
    ordinary "cannot resolve merge-base" degrade-loud path, not an error.
    Does NOT silently drop a foreign-consumer or malformed hit with no
    signal — both land in `warnings`.
    """
    warnings: list[str] = []

    merge_base_result = _run_git(["merge-base", "origin/main", "HEAD"], worktree_root)
    merge_base = merge_base_result.stdout.strip()
    if merge_base_result.returncode != 0 or not merge_base:
        warnings.append(
            "detect_git_provenance_consumed: could not resolve origin/main "
            "merge-base — Detector B (git-provenance chain-terminal "
            "detection) skipped; if this session shipped/archived a handoff, "
            "chain-terminal may be missed"
        )
        return [], warnings

    log_result = _run_git(
        [
            "log", f"{merge_base}..HEAD",
            "--diff-filter=ARC", "--find-renames", "--name-status",
            "--format=__SID__ %(trailers:key=Session-Id,valueonly)",
        ],
        worktree_root,
    )
    if log_result.returncode != 0:
        warnings.append(
            f"detect_git_provenance_consumed: git log over {merge_base}..HEAD "
            f"failed: {log_result.stderr.strip()}"
        )
        return [], warnings

    candidate_paths: list[str] = []
    current_sid: str | None = None
    for line in log_result.stdout.splitlines():
        if line.startswith("__SID__"):
            current_sid = line[len("__SID__"):].strip()
            continue
        if not line.strip() or current_sid != sid:
            continue
        # --name-status rows: "A\tpath" (added) or "R100\told\tnew" /
        # "C100\told\tnew" (renamed/copied) — the last tab-separated field is
        # always the CURRENT (destination) path.
        fields = line.split("\t")
        path = fields[-1].strip()
        if path.startswith("archive/handoffs/") and path.endswith(".md"):
            candidate_paths.append(path)

    hits: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for rel_path in candidate_paths:
        if rel_path in seen:
            continue
        seen.add(rel_path)

        abs_path = worktree_root / rel_path
        if not abs_path.exists():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(
                f"detect_git_provenance_consumed: could not read {rel_path}: {exc}"
            )
            continue
        fm = _parse_frontmatter_flat(text)

        # Oracle order (SKILL.md Step 0): spoof guard FIRST, well-formedness
        # only checked in its else-branch — a candidate that is BOTH
        # malformed AND foreign-consumed must surface the spoof diagnostic
        # (names the real consumer), not the generic "no predecessor:" one.
        # Ledger-first (C4, see docs/plans/2026-08-07-claim-state-ledger-first
        # -authoritative-read.md): a raw mirror-only read of claimed_by/
        # consumed_by DISARMS this guard on the branch-switch-revert desync
        # claim_state.py's module docstring names — the ledger still holds a
        # live foreign claim, the mirror reverted to unclaimed, and this
        # candidate would be silently adopted as sid's own predecessor.
        # resolve_claim_state is ledger-first with mirror fallback, so a
        # foreign ledger claim still fires this guard even when the mirror
        # disagrees.
        claim_state = resolve_claim_state(abs_path, repo_root=worktree_root)
        claimant = str(claim_state.holder or "").strip()
        if claimant and claimant != sid:
            warnings.append(
                f"detect_git_provenance_consumed: restoration-commit spoof "
                f"guard rejected {rel_path} — claimed_by names "
                f"{claimant}, not {sid}"
            )
            continue

        if "predecessor" not in fm:
            warnings.append(
                f"detect_git_provenance_consumed: well-formedness guard "
                f"rejected {rel_path} — no predecessor: line"
            )
            continue

        hits.append((rel_path, fm))

    return hits, warnings
