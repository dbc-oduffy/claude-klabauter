"""Engine-authored launcher script that starts every ready run of a multi-ask submission.

Writes one Workflow script plus its emission receipt. Never spawns a process and never fires
a run: the harness runs the script the caller's rewritten tool input points at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class LaunchChild:
    """One run the launcher emits and starts; emission happens inside the launcher, at child start."""

    run_id: str  # the fan-out's reserved run id
    sizing_rel: str  # the slice sizing, POSIX, repo-relative
    emit_params: Mapping[str, Any]  # dispatch.emit params the emit leg sends (ask, sizing_path, reserved_run_id, ...)


LaunchFn = Callable[[Path, str, Sequence[LaunchChild], Optional[str]], Path]


def launcher_rel(parent_id: str) -> str:
    """Repo-relative POSIX path of the launcher script for ``parent_id``."""
    return f"scratch/warp/{parent_id}.launcher.workflow.mjs"


def write_launcher(
    root: Path,
    parent_id: str,
    children: Sequence[LaunchChild],
    session_id: Optional[str],
) -> Path:
    """Write the launcher for ``children`` under ``root`` and return its absolute path.

    The script opens with ``export const meta`` and runs each child under ``parallel()``, so one
    child's failure resolves to null and never cancels a sibling. Per child: (1) emit leg, a haiku
    ``agent()`` running ``dispatch.emit`` with ``child.emit_params``, refusing a path outside
    ``scratch/warp/`` or a foreign run_id; (2) start, ``workflow()`` on the emitted script with
    ``LAUNCHER_CHILD_ARG`` true; (3) stage hand-back, on a ``STAGE_PENDING_KEY`` return the
    launcher reads the handle and re-starts the child with ``STAGED_MANIFEST_ARG`` set. Returns
    ``{parent_id, runs: [{run_id, status, result|error}]}``. An emission receipt is recorded.
    """
    raise NotImplementedError("C2")
