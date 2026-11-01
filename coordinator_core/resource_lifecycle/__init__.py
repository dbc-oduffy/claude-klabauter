"""coordinator_core.resource_lifecycle — closer registry, lease, bounded store, drain.

Public names resolve lazily so each submodule can land independently.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "CloserRegistry": "closers",
    "Lease": "lease",
    "BoundedStore": "store",
    "Drain": "drain",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)
