"""Per-call read-through scope for small repeated file reads and derived values.

A ``call_io_scope()`` block opens a fresh cache that lives for that ``with`` block only
(a ``contextvars.ContextVar``, never a process-lifetime cache), mirroring
``git_state.index_read_cache_scope``. Outside a scope every helper is a plain uncached
operation, so callers that never open one behave exactly as before.

Stdlib only: importing this adds nothing to the cold-import set.
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, Optional, Tuple, TypeVar

__all__ = [
    "HEAD_NAMESPACE",
    "call_io_scope",
    "scope_open",
    "read_bytes",
    "memo",
    "invalidate",
]

T = TypeVar("T")

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


@contextmanager
def call_io_scope() -> Iterator[None]:
    """Open an empty per-call cache for this block; a nested scope shadows the outer one."""
    token = _SCOPE.set(_Scope())
    try:
        yield
    finally:
        _SCOPE.reset(token)


def scope_open() -> bool:
    """True while a ``call_io_scope()`` block is active in this context."""
    return _SCOPE.get() is not None


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except _NOT_FOUND:
        return None


def read_bytes(path) -> Optional[bytes]:
    """Bytes of ``path``, or ``None`` when absent.

    Inside a scope the first read per path is cached (absence included); any other
    ``OSError`` propagates and is never cached. Outside a scope every call reads.
    """
    key = os.fspath(path)
    scope = _SCOPE.get()
    if scope is None:
        return _read(key)
    files = scope.files
    if key in files:
        return files[key]
    data = _read(key)
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
