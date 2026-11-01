"""Drain: wait only for work admitted before a transition."""

from __future__ import annotations

import threading
from typing import Optional


class Token:
    """Context-manager admission stamped with the generation it was admitted in."""

    def __init__(self, drain: "Drain", generation: int) -> None:
        self._drain = drain
        self.generation = generation
        self._released = False

    def release(self) -> None:
        """Mark the work finished; idempotent."""
        self._drain._release(self)

    def __enter__(self) -> "Token":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class Drain:
    """Tracks admitted work per generation.

    `transition()` bumps the generation and pins the tokens still in flight;
    `wait()` blocks until exactly those finish. Work admitted after a
    transition is never waited on by that transition's `wait()`.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._generation = 0
        self._in_flight: set[Token] = set()
        self._pending: set[Token] = set()

    @property
    def generation(self) -> int:
        with self._cond:
            return self._generation

    def admit(self) -> Token:
        """Admit one unit of work; use the token as a context manager or call release()."""
        with self._cond:
            token = Token(self, self._generation)
            self._in_flight.add(token)
            return token

    def transition(self) -> int:
        """Bump the generation, pin all in-flight older work, return the new generation."""
        with self._cond:
            self._generation += 1
            self._pending |= {t for t in self._in_flight if t.generation < self._generation}
            return self._generation

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until pre-transition work finishes; False if `timeout` elapsed first."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._pending, timeout)

    def _release(self, token: Token) -> None:
        with self._cond:
            if token._released:
                return
            token._released = True
            self._in_flight.discard(token)
            self._pending.discard(token)
            self._cond.notify_all()
