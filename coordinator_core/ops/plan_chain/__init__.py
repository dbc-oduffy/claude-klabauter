"""plan_chain: the sizing-to-commit chain driver (cold process, never a registered op)."""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "run":
        from .driver import run

        return run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
