"""Pure predicate: should engine-ask refuse a run rooted in a repo other than the session's.

The verdict is an assent check (G-EM group-em-b7): a peer-repo run is refused
unless the PM's assent is recorded as `approved`. It does not read the
write-bump guard's arming, so an unanchored or stood-down session is refused too.

Known over-refusal: an UNREGISTERED nested repo under the session anchor's
subtree is refused as a peer repo. That errs toward refusing before any write.

Pure: no spawn (callers pass already-resolved toplevels), no env read, no file write.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DENY_TEXT = (
    "engine-ask: run root {run_root} is not this session's repo {session_root}; "
    "a run there writes into a peer repo. Fire it from a session rooted in "
    "{run_root}, or add `cross_repo_approved: true` once the PM has assented."
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
    approved: bool,
) -> Optional[str]:
    """Return the deny text, or None to admit the run."""
    if session_root is None or research_route or approved:
        return None
    if _same_checkout(run_root, session_root):
        return None
    return DENY_TEXT.format(run_root=run_root, session_root=session_root)
