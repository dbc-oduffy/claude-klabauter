"""
coordinator_core.session.claimed_write — the claiming seam: four write entry
points that claim only what actually landed (D1,
docs/plans/2026-09-11-state-writers-claim-through-one-seam.md § C1).

Purpose: a raw write primitive (``open(..., "w")``, an atomic replace, a bare
exclusive-create, an append) carries no claim of its own — the claim is a
separate line (``declare_write(path)``) someone has to remember to write
right after it, and that remembering has already failed once (47 modules
call ``declare_write`` today; 83 more hold a raw write and a `state/` signal
and record no claim — the plan's census). This module makes claiming a
PROPERTY of the write call itself: each entry point below performs the write,
and ONLY on a successful landing, calls
``coordinator_core.session.declared_writes.declare_write(<path actually
written>)`` — never before the write, never on a path that turned out not to
be the one written (``create_exclusive(retry_suffix=True)``'s collision case).

The four entry points, one per live write primitive:
    - ``replace_text(path, text, *, encoding="utf-8", newline="\\n") -> Path``
    - ``replace_bytes(path, data) -> Path``
    - ``create_exclusive(path, text, *, retry_suffix=False, encoding="utf-8") -> Path``
    - ``append_claimed_line(path, encoded: bytes) -> Path``

Negative-spec (RAG-bait). The exact substrings named below are deliberately
NOT written literally elsewhere in this docstring or in this module's code
(``coordinator_core/tests/test_raw_writes_have_a_disposition.py``-adjacent
AC2 greps this file for each one and require a zero count, so this module
itself must never hold a second copy of any of the four things it explicitly
does not do):
    - **No recorder.** This module adds no recorder of its own. Every
      declaration reaches the one existing recorder in ``ipc.py`` through
      whichever collection is already open — ``ipc`` dispatch,
      ``cli_entry.run_op_main``/``recording_declared_writes``, or
      ``warm.entry_seam`` — never a second dialect inlined here. Outside an
      open collection, ``declare_write`` is a no-op (``declared_writes.py``'s
      own negative spec): the write still lands, nothing is claimed, and no
      session directory is created.
    - **No owner override.** A write whose claim belongs to a session OTHER
      than the caller (the subagent-provision class) is not this seam's
      job — that stays on the explicit-owner claim helper in
      ``session/scope.py``, with its own phantom-live-peer guard. This
      module never calls that helper and never reaches into the session
      scope module's touch API directly.
    - **Not for session-internal writes.** The touch record, ``meta.json``,
      locks, anything under ``.git/coordinator-sessions/``, and the
      recorder chain itself are NOT routed through this seam (they would
      recurse through the very claim machinery this seam feeds).
    - **Not for `WRITE_SURFACE` install writers.** `coordinator_core/install/`
      writers that declare a module-level `WRITE_SURFACE` are the install
      detector's own domain and are not migrated onto this seam.
    - **One replace primitive.** ``replace_bytes`` delegates to
      ``coordinator_core.atomic_replace.atomic_write_bytes`` and re-implements
      none of its temp-file-and-atomic-rename mechanics; ``replace_text`` is
      encode-with-newline-translation, then ``replace_bytes``. Neither
      function performs the temp-file creation or the atomic rename itself
      — see AC2.
    - **Windows note (D1).** The underlying atomic replace (delegated to,
      never re-implemented, above) fails on Windows when another handle
      holds the target open for reading without share-delete access. This
      module does not work around that difference; a caller who hits it
      stays on a raw write with a ``claims-explicitly`` disposition, per the
      plan's C3 table.
    - **Mode preservation.** ``replace_text``/``replace_bytes`` preserve an
      EXISTING target's mode (``atomic_write_bytes``'s ``preserve_mode``,
      default True) rather than silently narrowing it to the temp file's
      default 0600. A target that does not yet exist is created at that
      same default mode (0600 on POSIX). The underlying atomic replace also
      replaces a symlink itself rather than writing through it, and breaks
      hardlinks — both are named here, not worked around.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md § C1/D1
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from coordinator_core.atomic_append import append_line
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.session.declared_writes import declare_write

__all__ = ["replace_text", "replace_bytes", "create_exclusive", "append_claimed_line"]

# Mirrors ops/queue_append.py::_COLLISION_RETRY_CAP.
_COLLISION_RETRY_CAP = 1000


def replace_bytes(path: Union[str, Path], data: bytes) -> Path:
    """Atomically replace ``path`` with ``data`` and, if the write lands,
    declare exactly the path written.

    Delegates to :func:`coordinator_core.atomic_replace.atomic_write_bytes`
    for the write mechanics; this function adds only the claim. On any
    exception the write did not land (``atomic_write_bytes`` leaves
    ``path`` untouched and re-raises), so nothing is declared and the
    original exception propagates unchanged.
    """
    target = Path(path)
    atomic_write_bytes(target, data)
    declare_write(str(target))
    return target


def replace_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> Path:
    """Encode ``text`` (translating ``\\n`` to ``newline``) and replace
    ``path`` via :func:`replace_bytes`. See that function for the claim and
    failure contract; this function does no writing of its own."""
    encoded = text.replace("\n", newline).encode(encoding)
    return replace_bytes(path, encoded)


def create_exclusive(
    path: Union[str, Path],
    text: str,
    *,
    retry_suffix: bool = False,
    encoding: str = "utf-8",
) -> Path:
    """Create ``path`` exclusively (``O_CREAT | O_EXCL | O_WRONLY``) and, if
    the write lands, declare the path ACTUALLY written.

    Mirrors ``coordinator_core.ops.queue_append.py ::
    _write_out_path_excl``'s retry-suffix loop when ``retry_suffix=True``:
    on a ``FileExistsError`` collision, retries against ``<root>-<n><ext>``
    up to ``_COLLISION_RETRY_CAP`` attempts, and declares the SUFFIXED path
    that was actually created — never the originally-requested one.

    With ``retry_suffix=False`` (the default), a collision on an existing
    ``path`` re-raises ``FileExistsError`` immediately and declares nothing
    — no bytes were written, so there is nothing to claim.
    """
    target = Path(path)
    root, ext = os.path.splitext(str(target))
    candidate = str(target)
    attempt = 1
    encoded = text.encode(encoding)
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if not retry_suffix:
                raise
            attempt += 1
            if attempt > _COLLISION_RETRY_CAP:
                raise FileExistsError(
                    f"claimed_write.create_exclusive: refusing to drop entry — "
                    f"exhausted {_COLLISION_RETRY_CAP} collision-retry attempts "
                    f"for base path {str(target)!r}. Tried {str(target)!r} "
                    f"through {root!r}-{_COLLISION_RETRY_CAP}{ext!r}."
                ) from None
            candidate = f"{root}-{attempt}{ext}"
            continue
        try:
            view = memoryview(encoded)
            while view:
                n = os.write(fd, view)
                view = view[n:]
        finally:
            os.close(fd)
        break
    written = Path(candidate)
    declare_write(str(written))
    return written


def append_claimed_line(path: Union[str, Path], encoded: bytes) -> Path:
    """Atomically append ``encoded`` (already newline-terminated) to
    ``path`` via :func:`coordinator_core.atomic_append.append_line`
    (unchanged — this is the one append primitive, not a second copy of
    it) and, if the append lands, declare ``path``.

    On any exception the append did not fully land per ``append_line``'s
    own atomicity contract, so nothing is declared and the original
    exception propagates unchanged.
    """
    target = Path(path)
    append_line(target, encoded)
    declare_write(str(target))
    return target
