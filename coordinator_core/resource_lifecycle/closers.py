"""CloserRegistry — closers that fail stay owned and are retried on the next close()."""

from __future__ import annotations

import threading
from typing import Callable


class CloserRegistry:
    """Registry of named closers run LIFO by close().

    A closer that returns is dropped; one that raises stays registered so a later
    close() retries it. close() raises an ExceptionGroup of the failures.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closers: list[tuple[str, Callable[[], object]]] = []

    def register(self, name: str, fn: Callable[[], object]) -> None:
        with self._lock:
            self._closers.append((name, fn))

    def pending(self) -> list[str]:
        """Names still owned, in registration order."""
        with self._lock:
            return [name for name, _ in self._closers]

    def close(self) -> None:
        """Run owned closers LIFO; raise ExceptionGroup of those that raised.

        Closers run outside the lock so one may register or inspect the registry.
        """
        with self._lock:
            batch = self._closers
            self._closers = []
        failed: list[tuple[str, Callable[[], object]]] = []
        errors: list[Exception] = []
        remaining = list(reversed(batch))
        try:
            while remaining:
                name, fn = remaining[0]
                try:
                    fn()
                except Exception as exc:
                    exc.add_note(f"closer {name!r} failed")
                    failed.append((name, fn))
                    errors.append(exc)
                remaining.pop(0)
        finally:
            # A BaseException (e.g. KeyboardInterrupt) leaves unrun closers owned, not lost.
            failed.reverse()
            remaining.reverse()
            if failed or remaining:
                with self._lock:
                    self._closers = remaining + failed + self._closers
        if errors:
            raise ExceptionGroup("closers failed", errors)
