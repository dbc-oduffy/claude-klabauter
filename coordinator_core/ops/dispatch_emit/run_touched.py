"""
coordinator_core.ops.dispatch_emit.run_touched -- the paths one run itself touched.

Purpose: the terminal commit admits undeclared dirt only when it sits in this set, so a
sibling run of the same session in a shared directory is never staged.

Invariant: reads only inputs the commit already holds (request, inline review, run
artifacts); never consults session claims. Stdlib only at module top so the terminal
commit's cold-import pin is unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest


def _norm(path: object) -> Optional[str]:
    if not isinstance(path, str) or not path:
        return None
    return path.replace("\\", "/")


def touched_paths(
    request: "CommitRequest",
    inline_review: Optional[dict],
    artifact_paths: Iterable[str] = (),
) -> frozenset:
    """Repo-relative forward-slash paths THIS run touched: every chunk's non-empty
    ``report``, every string in ``inline_review["prep"]["slice_files"]`` when that is a
    list, and ``artifact_paths``. Malformed input contributes nothing and never raises.
    Unlike ``terminal_commit._reviewed_files_if_passed`` it does not require a passing review:
    a run's touched files are touched whether or not the review passed."""
    found: set = set()
    for chunk in getattr(request, "chunks", None) or ():
        found.add(_norm(getattr(chunk, "report", "")))
    prep = inline_review.get("prep") if isinstance(inline_review, dict) else None
    files = prep.get("slice_files") if isinstance(prep, dict) else None
    if isinstance(files, list):
        found.update(_norm(f) for f in files)
    try:
        found.update(_norm(p) for p in artifact_paths or ())
    except TypeError:
        pass
    found.discard(None)
    return frozenset(found)
