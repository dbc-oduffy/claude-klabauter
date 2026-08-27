"""
coordinator_core.session.declared_writes — one declare-write API that works on
both invocation paths.

Purpose: an op handler calls ``declare_write(path)`` for each file it ACTUALLY
wrote, and the resulting session scope-touch is recorded no matter how the op
was reached — as a subprocess through ``ipc.dispatch_message``, or as a plain
in-process import from a ``coordinator/bin/`` trampoline.

Why this exists (DR-276): ``dispatch_message`` is the sole process-level
dispatch chokepoint, and since 2026-08-04 it turns a handler's
``_scope_touch_paths`` self-report into a ``session.scope.touch()`` claim. A
CLI that imports ``coordinator_core.ops.<name>.main`` in-process never reaches
that chokepoint, so everything it writes carries no session claim, classifies as
``orphans`` in ``session.scope.compute_scope``, and is refused at the
``scoped_git_commit`` sink. Measured over the 368 non-test entrypoints under
``coordinator/bin/``: zero reach ``dispatch_message`` in-process; two record a
touch directly.

Design: this module owns ONLY the collection primitive — a context-local list
that ``declare_write`` appends to. It deliberately contains **no recorder**.
Both paths hand the collected list to ``ipc._record_self_reported_touches``,
the single existing recorder, so there is exactly one dialect of path
validation, cross-repo containment, capping, and fail-open behaviour in the
codebase. See ``coordinator_core/ipc.py``'s ``_SCOPE_TOUCH_PATHS_KEY`` contract
block for why a second recorder must never be inlined at a new call site.

Negative-spec (RAG-bait):
    ``declare_write`` outside an open collection is a NO-OP, not an error. A
    handler that declares nothing behaves exactly as it did before this module
    existed. The safe failure direction is under-coverage: a declaration can
    only WITHHOLD a path from other live sessions, never falsely grant one,
    because recording writes solely into the resolving session's own
    ``touched.txt``.

    This module does not resolve session identity, does not touch
    ``touched.txt``, and does not know what a repo root is. It is a list.

Spec backlink: docs/decisions/DR-276-operator-clis-record-session-writes-at-a.md
"""

from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from typing import Iterator, List, Optional

__all__ = ["declare_write", "collecting", "active_declarations"]

_ACTIVE: ContextVar[Optional[List[str]]] = ContextVar(
    "coordinator_core_declared_writes", default=None
)


def declare_write(path: object) -> None:
    """Declare that `path` was ACTUALLY written by the current op call.

    No-op when no collection is open (the pre-DR-276 behaviour, preserved for
    every caller that has not adopted the seam).

    `path` may be absolute or repo-relative; normalization, containment, and
    capping are the recorder's job, not this function's — deliberately, so that
    both invocation paths share one implementation of those rules.

    Never raises for an operational failure: a declaration that cannot be
    coerced to a non-empty string is dropped rather than failing the op that
    already succeeded.
    """
    active = _ACTIVE.get()
    if active is None:
        return
    if isinstance(path, (str, os.PathLike)):
        text = os.fspath(path)
    else:
        return
    if not text:
        return
    active.append(text)


@contextlib.contextmanager
def collecting(into: Optional[List[str]] = None) -> Iterator[List[str]]:
    """Open a declaration collection for the duration of the block.

    Yields the list that `declare_write` appends to. Nesting is supported: an
    inner collection shadows the outer one and the outer list is restored on
    exit, so a handler that itself invokes another handler cannot leak
    declarations upward.

    The yielded list object is stable for the life of the block, which is what
    makes this work across ``asyncio.to_thread`` — that call copies the context,
    so a rebind would be invisible to the caller, but appends to the same list
    object are not.
    """
    collected: List[str] = into if into is not None else []
    token = _ACTIVE.set(collected)
    try:
        yield collected
    finally:
        _ACTIVE.reset(token)


def active_declarations() -> Optional[List[str]]:
    """Return the open collection, or None when no collection is open.

    Exposed for the dispatch seam and for tests; handlers should call
    `declare_write` rather than reaching for the list.
    """
    return _ACTIVE.get()
