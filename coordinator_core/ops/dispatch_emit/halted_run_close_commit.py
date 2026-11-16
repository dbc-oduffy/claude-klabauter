"""The closure commit of a halted run: its record files plus ``Checkpoint-Run-Closed`` trailers in one scoped commit."""

from __future__ import annotations

import re
from pathlib import Path


def _landed_paths(root: Path, marker: dict):
    """Paths of the landing commit a ``landed`` marker names; ``None`` for any other marker or an unreadable sha."""
    sha = marker.get("sha")
    if marker.get("status") != "landed" or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    from coordinator_core.git.run import run_git

    done = run_git(["-c", "core.quotepath=off", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha], cwd=str(root))
    return done.stdout.split("\n") if done.returncode == 0 else None


def _closed_trailers(root: Path, run_id: str, record_dir: Path) -> str:
    """A blank line then one ``Checkpoint-Run-Closed:`` line per own checkpoint base, plus each open same-plan checkpoint whose withheld paths the run's own landing commit covers; ``""`` when none resolve."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines
    from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

    marker = halted_run._read(record_dir) or {}
    lines = run_closed_trailer_lines(
        closing_bases(
            root, run_id, {"run_base_sha": marker.get("run_base_sha")}, landed_paths=_landed_paths(root, marker),
        )
    )
    return "\n\n" + "\n".join(lines) if lines else ""


def commit_close_records(
    root: Path, run_id: str, record_dir: Path, subject: str, *,
    closed: bool = True, allow_empty: bool = False, extra_trailers: str = "",
) -> dict:
    """Commit the run's record files (halted.json, run-stage.json) plus every other changed or untracked path in *record_dir* (prompts/, resume.json) in one scoped commit headed by *subject*.

    *closed* appends ``Checkpoint-Run-Closed:`` trailers for the run's bases, then *extra_trailers*
    (newline-joined trailer lines, or ``""``). ``allow_empty`` anchors the commit on the run's
    ``manifest.json`` and lets it carry no diff, so the closure lands for a run with no record file to flip.

    Returns ``{"records_commit": sha}``; ``{}`` when nothing changed or the record dir is untracked
    scratch; ``{"records_commit_error": ...}`` on a refusal, which never fails the caller's marker write.
    """
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE
    from coordinator_core.ops.dispatch_emit.run_artifacts import record_dir_residue

    try:
        fixed = [
            (record_dir / name).relative_to(root).as_posix()
            for name in (halted_run.HALTED_FILE, RUN_STAGE_FILE)
            if (record_dir / name).is_file()
        ]
    except ValueError:
        return {}
    if allow_empty:
        fixed.append((record_dir / "manifest.json").relative_to(root).as_posix())
    elif not fixed or not fixed[0].startswith("state/"):
        return {}
    rels = sorted(set(fixed) | set(record_dir_residue(root, run_id)))
    tail = _closed_trailers(root, run_id, record_dir) if closed else ""
    if extra_trailers:
        tail += ("\n" if tail else "\n\n") + extra_trailers
    try:
        outcome = commit_paths(root, rels, subject + tail, allow_empty=allow_empty)
    except NothingToCommit:
        return {}
    except (CommitRefused, OSError) as exc:
        return {"records_commit_error": f"{type(exc).__name__}: {exc}"}
    return {"records_commit": outcome.sha}
