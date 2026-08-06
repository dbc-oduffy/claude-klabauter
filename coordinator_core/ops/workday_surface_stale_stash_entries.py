"""
coordinator_core.ops.workday_surface_stale_stash_entries — read-only stale
git-stash surfacing for the /workday-start ceremony.

Purpose: AC5 ("make recurrence detectable") of the unscoped-stash spinoff
(archive/handoffs/2026-07/2026-07-28-unscoped-stash-peer-sweep-data-loss.md).
Two abandoned-stash caches (19 entries, ~20,900 patch lines across two
repos) were both found by accident — one after three weeks. This is the
cheapest sufficient shape named in that baton: an age-thresholded stash
surface, wired to a daily ceremony so an accumulating cache is visible
within a day rather than weeks. No dashboard, no history/trend state, no
new ceremony — a single read plus one JSON payload.

Read-only, one git spawn: `git stash list --pretty=format:...`. `git
stash list` accepts the same log-format options as `git log -g
refs/stash` (it IS a `git log -g` walk over `refs/stash` under the hood),
so the reflog subject and the reflog timestamp both come back from the
ONE sanctioned command instead of needing a second `git log -g` call.
Never pops, applies, or drops anything.

Session attribution — a documented gap, not a silent omission: the
baton asks to also surface "any entry whose owning session is not the
current one, IF that is cheaply derivable from the stash message" and
explicitly forbids building session-attribution machinery if it is not
already sitting there. Every stash message surveyed across both repos
during the AC4 recovery audit (state/audits/2026-07-28-surviving-stash-
recovery-list.md) takes git's own two default shapes — "WIP on <branch>:
<sha> <subject>" or "On <branch>: <subject>" — neither of which carries a
session-id token; git's stash-message format has no field for one. That
machinery isn't sitting there, so this op surfaces age only.

Design-as-offers: `advice` leads with the safe forward action (inspect,
then scope future stashes) rather than a bare warning, per this repo's
"offers, not nags" doctrine — a consuming ceremony surfaces it verbatim,
never rephrased as a scold.

Contract: surface_stale_stash_entries(repo_root, threshold_days=7) ->
    {threshold_days: int, total: int,
     stale: [ {ref: str, age_days: int, subject: str}, ... ],
     advice: str, error: str|None}

Negative-spec:
    - NO `git stash show`, NO separate `git log -g` call, NO `git
      reflog` — one `git stash list` spawn carries everything.
    - NO pop/apply/drop/clear anywhere in this module.
    - Never raises on stash-list CONTENT — an unparseable line (wrong
      field count, non-integer timestamp) is skipped, matching the
      malformed-line-degrades-quietly rule this repo's sibling
      workday-surface op (workday_surface_auto_push_failure_stats) uses.
      Raises only on a missing/non-directory `repo_root` (premise
      failure) — a non-repo `repo_root` (the `git stash list` spawn
      itself fails) degrades to the healthy empty state with `error`
      set, since an advisory surface must never block a ceremony.
    - Advisory only — no field in the return value is a pass/fail gate.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Unit separator — stash subjects routinely contain literal "|" (cross-repo
# memo subjects, commit messages quoting shell pipelines), so a printable
# delimiter would silently misparse; \x1f never appears in a reflog subject.
_FIELD_SEP = "\x1f"

_SECONDS_PER_DAY = 86400

ADVICE = (
    "Inspect with `git stash show -p <ref>`; scope future stashes with "
    "`git stash push -u -- <paths>`."
)


class StaleStashEntriesError(RuntimeError):
    """Structured failure for a caller premise failure (missing repo_root)."""


def _run_stash_list(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "stash", "list", f"--pretty=format:%gd{_FIELD_SEP}%at{_FIELD_SEP}%gs"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )


def _parse_line(line: str, now: datetime, threshold_days: int) -> dict | None:
    """Parse one `git stash list` line; return a stale-entry dict or None.

    Returns None both when the entry is fresh AND when the line is
    malformed — the caller distinguishes those via the raw line count for
    `total`, this helper's only job is "is this a stale entry to report."
    """
    parts = line.split(_FIELD_SEP)
    if len(parts) != 3:
        return None  # malformed line — degrade quietly
    ref, at_raw, subject = parts
    try:
        at = int(at_raw)
    except ValueError:
        return None  # malformed timestamp — degrade quietly

    age_seconds = (now - datetime.fromtimestamp(at, tz=timezone.utc)).total_seconds()
    if age_seconds < threshold_days * _SECONDS_PER_DAY:
        return None  # fresh
    return {
        "ref": ref,
        "age_days": int(age_seconds // _SECONDS_PER_DAY),
        "subject": subject,
    }


def surface_stale_stash_entries(repo_root: str, threshold_days: int = 7) -> dict:
    """Surface stash entries older than `threshold_days` (default 7).

    Read-only throughout. `repo_root` missing/non-directory is a caller
    premise failure and raises `StaleStashEntriesError`; every other
    failure mode (no git worktree at `repo_root`, an empty stash, a
    malformed line) degrades to a healthy/quiet result — this is an
    advisory surface, never a gate.
    """
    root = Path(repo_root)
    if not root.is_dir():
        raise StaleStashEntriesError(
            "workday.surface_stale_stash_entries: repo_root "
            f"{str(root)!r} does not exist or is not a directory"
        )

    proc = _run_stash_list(root)
    if proc.returncode != 0:
        return {
            "threshold_days": threshold_days,
            "total": 0,
            "stale": [],
            "advice": ADVICE,
            "error": proc.stderr.strip() or "git stash list failed",
        }

    lines = [ln for ln in proc.stdout.splitlines() if ln]
    now = datetime.now(timezone.utc)
    stale = [
        entry
        for entry in (_parse_line(ln, now, threshold_days) for ln in lines)
        if entry is not None
    ]

    return {
        "threshold_days": threshold_days,
        "total": len(lines),
        "stale": stale,
        "advice": ADVICE,
        "error": None,
    }
