"""Per-call read-through scope for small repeated file reads and derived values.

A ``call_io_scope()`` block opens a fresh cache that lives for that ``with`` block only
(a ``contextvars.ContextVar``, never a process-lifetime cache), mirroring
``git_state.index_read_cache_scope``. Outside a scope every helper is a plain uncached
operation, so callers that never open one behave exactly as before.

Imports only `contextvars` and `os` at runtime: `typing` and `contextlib` would add ~20 modules to
the cold-import set of `coordinator_core.ipc`, which is ceilinged.
"""

from __future__ import annotations

import contextvars
import os

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Callable, Dict, Optional, Tuple, TypeVar

    T = TypeVar("T")

__all__ = [
    "HEAD_NAMESPACE",
    "HALT_READ_DEADLINE_SECS",
    "bounded_reads",
    "bounded_path_read",
    "call_io_scope",
    "scope_open",
    "read_bytes",
    "memo",
    "invalidate",
]

#: Memo namespace for resolved HEAD/ref values; `head_sha` memoises in it. Only the in-process
#: `cas_ref`/`cas_head_symref` invalidate it: a ref moved by a git subprocess or a peer inside an
#: open scope is not seen (use `head_sha(fresh=True)` there).
HEAD_NAMESPACE = "head-sha"

_NOT_FOUND = (FileNotFoundError, NotADirectoryError)


class _Scope:
    __slots__ = ("files", "memos")

    def __init__(self) -> None:
        self.files: Dict[str, Optional[bytes]] = {}
        self.memos: Dict[Tuple[str, str], object] = {}


_SCOPE: "contextvars.ContextVar[Optional[_Scope]]" = contextvars.ContextVar(
    "_call_io_scope", default=None
)


class call_io_scope:
    """Open an empty per-call cache for this block; a nested scope shadows the outer one."""

    __slots__ = ("_token",)

    def __enter__(self) -> None:
        self._token = _SCOPE.set(_Scope())

    def __exit__(self, *exc_info: object) -> None:
        _SCOPE.reset(self._token)


def scope_open() -> bool:
    """True while a ``call_io_scope()`` block is active in this context."""
    return _SCOPE.get() is not None


#: Per-read deadline inside ``bounded_reads()``. Answers: how long may one halt-commit file read
#: stall before the op parks retry-safe instead of holding the 10s ``LAND_BUDGET_SECS``? A halt
#: path makes many reads in sequence, so each gets the 500ms brightline's half (the
#: ``bounded_io.READ_DEADLINE_SECS`` value), never a share of the land budget.
HALT_READ_DEADLINE_SECS = 0.25

#: Deadline seconds while a ``bounded_reads()`` block is open, else ``None``.
_BOUNDED_DEADLINE: "contextvars.ContextVar[Optional[float]]" = contextvars.ContextVar(
    "_call_io_scope_bounded_deadline", default=None
)


class bounded_reads:
    """Within this block every ``read_bytes`` is bounded and raises ``IoIndeterminate`` on a stall.

    The raise is never cached (a later read of the path retries). Absent files still read as
    ``None``. Orthogonal to ``call_io_scope``: it bounds reads with or without a cache scope open.
    """

    __slots__ = ("_deadline", "_token")

    def __init__(self, deadline_secs: float = HALT_READ_DEADLINE_SECS) -> None:
        self._deadline = deadline_secs

    def __enter__(self) -> None:
        self._token = _BOUNDED_DEADLINE.set(self._deadline)

    def __exit__(self, *exc_info: object) -> None:
        _BOUNDED_DEADLINE.reset(self._token)


def bounded_deadline() -> Optional[float]:
    """The deadline seconds of the open ``bounded_reads()`` block, else ``None``."""
    return _BOUNDED_DEADLINE.get()


def _read(path: str, bounded: bool = False) -> Optional[bytes]:
    deadline = _BOUNDED_DEADLINE.get()
    if bounded or deadline is not None:
        # lazy: bounded_io pulls threading, which the cold-import ceiling excludes
        from coordinator_core.bounded_io import READ_DEADLINE_SECS, read_bytes_bounded

        return read_bytes_bounded(path, deadline=READ_DEADLINE_SECS if deadline is None else deadline)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except _NOT_FOUND:
        return None


def bounded_path_read(path, *, deadline_secs: Optional[float] = None) -> Optional[bytes]:
    """Uncached bounded bytes of ``path``, or ``None`` when absent; never reads unbounded.

    For callers that read via ``Path.read_bytes``/``stat`` today. Uses the open
    ``bounded_reads`` deadline, else ``deadline_secs``, else ``HALT_READ_DEADLINE_SECS``.
    Raises ``IoIndeterminate`` on a stall.
    """
    from coordinator_core.bounded_io import read_bytes_bounded

    deadline = deadline_secs if deadline_secs is not None else _BOUNDED_DEADLINE.get()
    return read_bytes_bounded(
        os.fspath(path), deadline=HALT_READ_DEADLINE_SECS if deadline is None else deadline
    )


def read_bytes(path, *, bounded: bool = False) -> Optional[bytes]:
    """Bytes of ``path``, or ``None`` when absent.

    Inside a scope the first read per path is cached (absence included); any other
    ``OSError`` propagates and is never cached. Outside a scope every call reads.
    ``bounded=True`` (or an open ``bounded_reads()``) reads through
    ``bounded_io.read_bytes_bounded`` (a worker thread per read: for hot marker files only);
    its ``IoIndeterminate`` propagates and is never cached.
    """
    key = os.fspath(path)
    scope = _SCOPE.get()
    if scope is None:
        return _read(key, bounded)
    files = scope.files
    if key in files:
        return files[key]
    data = _read(key, bounded)
    files[key] = data
    return data


def memo(namespace: str, key: str, compute: Callable[[], T]) -> T:
    """``compute()`` once per ``(namespace, key)`` per scope; outside a scope, always call it.

    An exception from ``compute`` propagates and is not cached.
    """
    scope = _SCOPE.get()
    if scope is None:
        return compute()
    slot = (namespace, key)
    memos = scope.memos
    if slot in memos:
        return memos[slot]  # type: ignore[return-value]
    value = compute()
    memos[slot] = value
    return value


def invalidate(namespace: Optional[str] = None, path=None) -> None:
    """Drop cached state in the open scope; a no-op outside one.

    ``namespace`` drops that memo namespace, ``path`` drops one cached file read,
    neither drops everything.
    """
    scope = _SCOPE.get()
    if scope is None:
        return
    if namespace is None and path is None:
        scope.files.clear()
        scope.memos.clear()
        return
    if namespace is not None:
        for slot in [s for s in scope.memos if s[0] == namespace]:
            del scope.memos[slot]
    if path is not None:
        scope.files.pop(os.fspath(path), None)
