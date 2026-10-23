"""Pure predicate: should engine-ask refuse a run rooted in a repo this session cannot write.

Invariant: the verdict must stay in step with the write-bump guard's own
applicability (`bump_applies` plus the environment stand-down), so a session
the guard does not bump is never refused.

Known over-refusal: the guard admits an UNREGISTERED nested repo under the
session anchor's subtree (`_verdict_bumps` -> `anchor_subtree_contains`); this
predicate refuses it. That errs toward refusing before any write, never toward
a strand.

Pure: no git spawn (callers pass already-resolved toplevels), no file write.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DENY_TEXT = (
    "engine-ask: run root {run_root} is not this session's repo {session_root}; "
    "its agents' writes there are refused by bump-out-of-repo-tool-write. "
    "Fire it from a session rooted in {run_root}."
)


def _same_checkout(a: Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(
        str(Path(b).resolve())
    )


def cross_repo_run_refusal(
    run_root: Path,
    session_root: Optional[Path],
    *,
    research_route: bool,
    session_id: Optional[str],
    cwd: Optional[str],
    env: Optional[dict],
) -> Optional[str]:
    """Return the deny text, or None to admit the run."""
    if session_root is None or research_route or session_id is None:
        return None
    if _same_checkout(run_root, session_root):
        return None
    from coordinator_core.bash_guards._write_bump_applicability import bump_applies
    from coordinator_core.bash_guards._write_bump_stand_down import (
        environment_stands_the_bump_down,
    )

    if not bump_applies(session_id, cwd, env):
        return None
    if environment_stands_the_bump_down(env) is not None:
        return None
    return DENY_TEXT.format(run_root=run_root, session_root=session_root)
