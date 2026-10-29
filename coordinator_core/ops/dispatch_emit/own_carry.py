"""Follow-on commit params for a run's own left-behind claims.

The run's product commit carries only declared paths; the committing session's
own left-behind claims land in their own attributed commit. The follow-on
carries only landable paths. Stdlib-only at
module top: terminal_commit imports this lazily and its cold-import pin must
stay green.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Final, Optional

REPLY_KEY: Final = "own_left_behind_commit"
TRAILER: Final = "Own-Left-Behind-Of"

_SHA40 = re.compile(r"[0-9a-fA-F]{40}")


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def own_carry_params(
    left: Sequence[str],
    contested: Collection[str],
    product_sha: str,
    session_id: str,
    *,
    ignored: Optional[Collection[str]] = None,
) -> Optional[dict]:
    """Build the commit_v2 params for the landable, uncontested part of ``left``.

    Machine-local paths and every path in ``ignored`` are dropped before the
    contested subtraction. Returns None when nothing landable remains. Passing
    ``ignored`` (a complete ignore answer for ``left``) adds
    ``"ignored_untracked": []`` so commit_v2 skips its check-ignore probe; with
    ``ignored=None`` the key is omitted. Raises ValueError when ``product_sha``
    is not 40 hex chars or ``session_id`` is empty. Pure: no git, no disk.
    Paths compare and emit with "/" separators.
    """
    from coordinator_core.git.machine_local_paths import machine_local_paths

    if not isinstance(product_sha, str) or not _SHA40.fullmatch(product_sha):
        raise ValueError(f"product_sha must be 40 hex chars: {product_sha!r}")
    if not session_id:
        raise ValueError("session_id must be non-empty")
    held = {_norm(p) for p in contested}
    drop = {_norm(p) for p in machine_local_paths(left)}
    if ignored is not None:
        drop |= {_norm(p) for p in ignored}
    remaining = sorted({_norm(p) for p in left} - held - drop)
    if not remaining:
        return None
    params = {
        "paths": remaining,
        "message": (
            "chore(own-left-behind): land this session's own claimed writes\n\n"
            f"{TRAILER}: {product_sha}\nSession-Id: {session_id}"
        ),
        "carry_own_left_behind": False,
        "carry_stopped_runs": False,
    }
    if ignored is not None:
        params["ignored_untracked"] = []
    return params
