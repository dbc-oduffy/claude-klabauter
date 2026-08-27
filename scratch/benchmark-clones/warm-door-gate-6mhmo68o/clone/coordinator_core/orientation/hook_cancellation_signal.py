"""
coordinator_core.orientation.hook_cancellation_signal — hook-cancellation miss-rate probe.

Purpose: DR-310 (``docs/decisions/DR-310-preflight-guards-are-best-effort-no-comm.md``)
ratifies, as ACCEPTED, the residual that a ``PreToolUse:Bash`` guard chain the harness
cancels mid-flight emits NO verdict at all — the command then runs fully unguarded, and
nobody is told. That DR explicitly declines to re-open the residual and explicitly leaves
open whether the miss should be made *visible*. This module is that visibility: it counts
cancelled ``PreToolUse:Bash`` attachments in this repo's own transcripts and reports a
RATE, never a fix, never a gate — see this repo's plan
``docs/plans/2026-08-15-hook-cancellation-miss-rate-signal.md`` Anti-scope.

DENOMINATOR CHOICE (defended, per that plan's C1 instruction): a bare cancellation count
answers nothing on its own — DR-310's own "94" is meaningless without knowing 94 OF WHAT.
The denominator here is **Bash ``tool_use`` entries** in the same scanned transcripts: each
one is exactly one occasion a ``PreToolUse:Bash`` hook chain was expected to run and could
have been cancelled. This is precisely the population the cancellation count is drawn from
(a cancellation is only ever attached to a Bash tool_use call), so the ratio is a true
miss-RATE over its own event space, not a count relative to an unrelated total (session
count, transcript-line count, wall-clock time) that would inflate or deflate for reasons
having nothing to do with guard delivery.

SCOPE (per plan Out-of-scope: "This repo's corpus only" — never fleet-wide): Claude Code
transcripts live under ``<claude-home>/projects/<encoded-cwd>/<session>.jsonl`` — one
directory per distinct invocation cwd, name-encoded by replacing every path separator and
drive-letter colon with ``-`` (the same encoding ``coordinator_core.ops.
decode_claude_projects_dir`` already documents as heuristic and version-drifting). This
module computes that encoding once, for THIS repo's root only, and scans only the matching
project directory — never the sibling-repo directories that same transcripts root also
holds. A directory that does not exist (fresh install, no sessions yet, or an encoding this
harness version does not produce) degrades to "no signal" (denominator 0, section omitted),
never a raise and never a fleet-wide fallback scan.

BOUNDED WORK (AC3 — "cannot itself become the hazard"): this repo's OWN transcript corpus
already measures in the hundreds of files and ~700+ MB (grepped 2026-08-15, no committed
snapshot — see ``HOOK_CANCEL_SCAN_BUDGET_BYTES`` below) and grows without limit, one file
per session, forever. An unbounded full-corpus scan measured ~5s wall-clock / ~740MB read
for this repo alone at time of writing — acceptable once, not a bound. This module instead
walks transcripts NEWEST-FIRST (by mtime) and stops once ``HOOK_CANCEL_SCAN_BUDGET_BYTES``
of file bytes would be exceeded, exactly the "cap the recent window, not the whole history"
discipline ``RECENT_COMMITS_MAX`` already applies to git log for the identical reason (see
that constant's docstring in ``regenerate_cache.py``) — a rolling miss-rate signal wants
what's representative of CURRENT delivery health, not an ever-growing historical audit.
Measured at the chosen budget (2026-08-15, this repo): ~55 of 401 transcripts, ~0.6s
wall-clock. Never a subprocess spawn — plain file I/O only, so this buys none of the
6-second git-probe budget DR-310's own mitigation (``_GIT_PROBE_BUDGET_SECONDS``) protects
(see plan Anti-scope: "do NOT add subprocess probes").

Never runs on any ``PreToolUse`` path — this is a cold, ceremony/machine-invoked call from
``orientation.regenerate_cache.build_cache`` only, the same cold-path posture that module's
own docstring already claims for itself.

Fail-open throughout, matching every ``emit_*`` helper in ``regenerate_cache.py``: a
missing projects root, missing per-repo directory, unreadable file, or unparseable line is
skipped, never raised. A corpus with zero Bash tool_use calls (denominator 0) yields an
undefined rate — the section is omitted entirely rather than rendering a rate over an empty
population (matching every other omit-when-empty section in ``regenerate_cache.py``).

Spec backlink: docs/plans/2026-08-15-hook-cancellation-miss-rate-signal.md § C1
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core._settings_home import home_dir
from coordinator_core.win_portability import same_path

#: The one hook chain DR-310 is about — see plan Out-of-scope: "Cancellations of hooks
#: other than PreToolUse:Bash, unless they fall out for free." They do not fall out for
#: free here (the denominator itself is Bash-tool_use-shaped), so this stays a hard filter.
_TARGET_HOOK_NAME = "PreToolUse:Bash"

HOOK_CANCEL_SCAN_BUDGET_BYTES = 64 * 1024 * 1024
"""Hard cap on total transcript bytes read per scan, across however many of this repo's
own newest-first transcripts fit under it. See module docstring's BOUNDED WORK paragraph
for the measurement this was picked from (~0.6s / ~55 files at this value, against a
401-file / ~740MB full corpus at time of writing) — retune here if the measured cost drifts
with corpus growth, not by feel."""


@dataclass(frozen=True)
class HookCancellationRate:
    """One scan's result. ``denominator`` is Bash tool_use entries seen in the scanned
    (budget-bounded) window; ``cancelled`` is PreToolUse:Bash cancellation attachments seen
    in that same window. ``rate`` is ``None`` when ``denominator`` is 0 (undefined, not
    zero) -- see module docstring's fail-open paragraph."""

    cancelled: int
    denominator: int
    scanned_files: int
    scanned_bytes: int

    @property
    def rate(self) -> Optional[float]:
        if self.denominator == 0:
            return None
        return self.cancelled / self.denominator


def _encode_project_dir_name(repo_root: Path) -> str:
    """Replicate Claude Code's ``<claude-home>/projects/`` directory-name encoding for
    *repo_root*: every path separator and drive-letter colon becomes ``-``. Heuristic, by
    the same admission ``coordinator_core.ops.decode_claude_projects_dir`` already makes
    for the inverse direction -- encoding has drifted across harness versions there, and
    may again. A miss here degrades to "no directory found" (see
    ``_project_transcripts_dir``), never a wrong-repo scan."""
    raw = str(repo_root)
    out = []
    for ch in raw:
        if ch in (":", "\\", "/"):
            out.append("-")
        else:
            out.append(ch)
    return "".join(out)


def _project_transcripts_dir(repo_root: Path) -> Optional[Path]:
    """Resolve the single ``<claude-home>/projects/<encoded-repo-root>`` directory for
    *repo_root*, or None when it does not exist -- never a fleet-wide fallback scan (see
    module docstring's SCOPE paragraph)."""
    projects_root = home_dir() / ".claude" / "projects"
    candidate = projects_root / _encode_project_dir_name(repo_root)
    if candidate.is_dir():
        return candidate
    return None


def _select_bounded_transcripts(
    project_dir: Path, budget_bytes: int
) -> Tuple[List[Path], int]:
    """Return (paths, total_bytes) for *project_dir*'s ``*.jsonl`` transcripts, newest
    mtime first, stopping once *budget_bytes* would be exceeded (module docstring's BOUNDED
    WORK paragraph). Always includes at least the single newest file, even if it alone
    exceeds the budget -- a budget of zero usable files would silently under-report rather
    than bound anything."""
    entries: List[Tuple[float, Path, int]] = []
    try:
        for p in project_dir.glob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, p, st.st_size))
    except OSError:
        return [], 0

    entries.sort(key=lambda e: e[0], reverse=True)

    selected: List[Path] = []
    total = 0
    for _, path, size in entries:
        if selected and total + size > budget_bytes:
            break
        selected.append(path)
        total += size
    return selected, total


def _line_matches_repo(last_cwd: Optional[str], repo_root: Path) -> bool:
    """True when *last_cwd* (the most recent ``cwd`` field seen in this transcript) is
    this repo's root, or when no ``cwd`` has been seen yet (fail open: count rather than
    silently drop when the field is simply absent from a line, matching this module's
    fail-open posture elsewhere)."""
    if last_cwd is None:
        return True
    try:
        return same_path(last_cwd, str(repo_root))
    except (OSError, ValueError):
        return False


def _scan_one_transcript(path: Path, repo_root: Path) -> Tuple[int, int]:
    """Stream *path* line-by-line, returning (cancelled, denominator) counts for lines
    whose most-recently-seen ``cwd`` matches *repo_root*. Never raises -- an unreadable
    file or an unparseable line is skipped, matching every other transcript reader in this
    tree (e.g. ``session.receiver_state.reduce_transcript_tail``)."""
    cancelled = 0
    denominator = 0
    last_cwd: Optional[str] = None
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    with fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue

            cwd = rec.get("cwd")
            if isinstance(cwd, str) and cwd:
                last_cwd = cwd
            if not _line_matches_repo(last_cwd, repo_root):
                continue

            if rec.get("type") == "attachment":
                attachment = rec.get("attachment")
                if (
                    isinstance(attachment, dict)
                    and attachment.get("type") == "hook_cancelled"
                    and attachment.get("hookName") == _TARGET_HOOK_NAME
                ):
                    cancelled += 1
                continue

            message = rec.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Bash"
                ):
                    denominator += 1
    return cancelled, denominator


def scan_hook_cancellation_rate(
    repo_root: Path, *, budget_bytes: int = HOOK_CANCEL_SCAN_BUDGET_BYTES
) -> HookCancellationRate:
    """Scan *repo_root*'s own transcripts (bounded per module docstring) and return the
    cancelled/denominator/rate result. Never raises: a missing/unreadable corpus resolves
    to ``HookCancellationRate(0, 0, 0, 0)`` (rate ``None``, section omitted by the caller)."""
    project_dir = _project_transcripts_dir(repo_root)
    if project_dir is None:
        return HookCancellationRate(0, 0, 0, 0)

    paths, total_bytes = _select_bounded_transcripts(project_dir, budget_bytes)
    cancelled = 0
    denominator = 0
    for path in paths:
        c, d = _scan_one_transcript(path, repo_root)
        cancelled += c
        denominator += d
    return HookCancellationRate(cancelled, denominator, len(paths), total_bytes)


def emit_hook_cancellation_rate(repo_root: Path) -> str:
    """Render the ``## Hook cancellation miss rate`` section's single body line, or ``""``
    to omit the section entirely (matching every other omit-when-empty section in
    ``regenerate_cache.py``) when the denominator is 0 -- no Bash tool_use calls seen in
    the scanned window, so a rate is undefined, not zero.

    Deliberately its OWN section, never folded into ``## Housekeeping`` (plan Anti-scope):
    Housekeeping renders faults, and DR-310 ratifies this residual as an ACCEPTED cost —
    presenting it as a failure every session boot would re-open by presentation what that
    decision closed."""
    result = scan_hook_cancellation_rate(repo_root)
    if result.rate is None:
        return ""
    pct = result.rate * 100
    return (
        f"- {pct:.1f}% cancelled ({result.cancelled}/{result.denominator} "
        f"`{_TARGET_HOOK_NAME}` Bash calls, last {result.scanned_files} transcript(s) "
        f"scanned) — DR-310 accepted residual, informational only"
    )
