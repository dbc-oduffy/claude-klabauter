"""
coordinator_core.session.path_dialect — the one subprocess-free path
canonicalizer shared by ``coordinator_core.session.scope`` and
``coordinator_core.session.claim_index``.

Plan: docs/plans/2026-08-11-claim-release-and-the-gate-that-cannot-clear.md
(chunk C1).

Purpose: both ``scope.py::normalize_touch_path``'s relative arm (previously
absent — see that function's own history) and
``claim_index.py::_normalize_key`` need to fold a caller-supplied,
possibly-backslashed relative pathspec into the SAME dialect
``touched.txt`` keys are written in, without spawning ``git`` or importing
anything that might. Before this module existed, ``claim_index.py`` kept an
inline copy of this expression specifically because no such standalone,
subprocess-free callable existed to import — see that module's (now
historical) docstring paragraph on ``_normalize_key``. This module
discharges that premise structurally: it imports only ``posixpath``.

Negative-spec: do NOT import ``subprocess``, ``coordinator_core.session.core``,
or ``coordinator_core.session.scope`` here — any of those would reintroduce
the git-spawn dependency this module exists to avoid on
``claim_index.py``'s pure-file-read performance premise (see that module's
NO GIT SUBPROCESS SPAWNS paragraph). Do NOT re-inline a further copy of this
expression at a new call site; import and call this.
"""

from __future__ import annotations

import posixpath


def canonicalize_relative_path(path: str) -> str:
    """Canonicalize ``path`` to the SAME dialect ``touched.txt`` is written
    in: separator-normalize (backslash -> forward slash), then
    ``posixpath.normpath``.

    This is the exact expression previously duplicated inline at
    ``scope.py::classify_touch_entry`` and ``claim_index.py::_normalize_key``
    — ``posixpath.normpath(path.replace("\\\\", "/"))``. Callers needing a
    CONTAINMENT verdict (is the result still inside the worktree?) must
    apply their own check on the returned value; this function performs no
    absoluteness or containment classification of its own — it is a pure
    string transform, deliberately unopinionated about what the caller does
    with the result.
    """
    return posixpath.normpath(path.replace("\\", "/"))
