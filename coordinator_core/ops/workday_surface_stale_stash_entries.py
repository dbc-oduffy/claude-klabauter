
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_FIELD_SEP = "\x1f"

_SECONDS_PER_DAY = 86400

ADVICE = (
    "Inspect with `git stash show -p <ref>`; scope future stashes with "
    "`git stash push -u -- <paths>`."
)


class StaleStashEntriesError(RuntimeError):
    pass


def _run_stash_list(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "stash", "list", f"--pretty=format:%gd{_FIELD_SEP}%at{_FIELD_SEP}%gs"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _parse_line(line: str, now: datetime, threshold_days: int) -> dict | None:
    parts = line.split(_FIELD_SEP)
    if len(parts) != 3:
        return None
    ref, at_raw, subject = parts
    try:
        at = int(at_raw)
    except ValueError:
        return None

    age_seconds = (now - datetime.fromtimestamp(at, tz=timezone.utc)).total_seconds()
    if age_seconds < threshold_days * _SECONDS_PER_DAY:
        return None
    return {
        "ref": ref,
        "age_days": int(age_seconds // _SECONDS_PER_DAY),
        "subject": subject,
    }


def surface_stale_stash_entries(repo_root: str, threshold_days: int = 7) -> dict:
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
