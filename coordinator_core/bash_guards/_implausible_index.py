"""Plausibility probe for the staged-deletion read in check_validate_commit."""

from __future__ import annotations

import os
from typing import Callable, List, Mapping, Optional, Tuple

_IMPLAUSIBLE_PROBE_GATE = 200

# The probe inherits the hook's environment; an inherited redirect is the
# likeliest cause of an empty/alternate index read, so the note names it.
_PROBE_ENV_KEYS = ("GIT_INDEX_FILE", "GIT_DIR")


def _probe_env_fragment(env: Mapping[str, str]) -> str:
    return " ".join("%s=%s" % (k, env.get(k) or "unset") for k in _PROBE_ENV_KEYS)


def implausible_deletion_note(
    status_lines: Optional[List[str]],
    cwd: Optional[str],
    run_git: Callable[..., Tuple[int, str]],
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """One terse line when the staged-deletion probe read an index that cannot
    be the real one (every HEAD entry "deleted", or an empty index under a
    populated HEAD); ``None`` when plausible. Spawns only past
    ``_IMPLAUSIBLE_PROBE_GATE`` ``D`` records, so ordinary commits pay nothing.
    The note carries the inherited ``GIT_INDEX_FILE``/``GIT_DIR`` (``env``,
    default ``os.environ``) so a redirected index is pinned from the line alone.
    """
    if not status_lines:
        return None
    deleted = sum(1 for l in status_lines if l.startswith("D\t"))
    if deleted < _IMPLAUSIBLE_PROBE_GATE:
        return None
    rc_h, head_out = run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd, timeout=5.0)
    if rc_h != 0:
        return None
    head_n = sum(1 for l in head_out.splitlines() if l)
    rc_i, index_out = run_git(["ls-files"], cwd, timeout=5.0)
    index_n = sum(1 for l in index_out.splitlines() if l) if rc_i == 0 else None
    if (head_n and deleted >= head_n) or (index_n == 0 and head_n > 0):
        return (
            "staged-deletion probe read an implausible index (%d of %d tracked "
            "deleted; %s); not checked"
            % (deleted, head_n, _probe_env_fragment(os.environ if env is None else env))
        )
    return None
