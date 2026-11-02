"""Box memory interface: pressure classification, storm count, remedy text, trim rate limit.

`pressure` holds the pure functions the watch (C1) and the admission guard (C3) share.
`orphan_reap` (C2) implements the two signatures declared here:

    select(census: Sequence[ProcRow], primitives: ReapPrimitives) -> list[ProcRow]
        grep/find rows whose parent is verified dead (absent from census, or created after the
        child). Unreadable parent counts as alive.
    reap(primitives: ReapPrimitives) -> list[int]
        terminate what `select` returns over a fresh census; return the reaped PIDs.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Protocol, Sequence


class ReapPrimitives(Protocol):
    """The `_host_probe.HostPrimitives` surface `orphan_reap` consumes."""

    def alive(self, pid: int) -> bool: ...

    def creation_time(self, pid: int) -> Optional[int]: ...

    def snapshot(self) -> Optional[Sequence]: ...


SelectFn = Callable[[Sequence, ReapPrimitives], List]
ReapFn = Callable[[ReapPrimitives], List[int]]
