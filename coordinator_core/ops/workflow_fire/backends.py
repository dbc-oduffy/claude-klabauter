"""Closed set of engine-ask backends, derived from the stage-family set.

Pure: no subprocess, no git. No default backend lives here.
"""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.stage_family import FAMILIES

BACKENDS = ("codex", "claude", "grok", "agy")

assert set(BACKENDS) == set(FAMILIES), "BACKENDS drifted from stage_family.FAMILIES"


def check(backend: str) -> str:
    """Return `backend` unchanged when it is a member of BACKENDS.

    Raises ValueError("backend must be one of codex|claude|grok|agy") otherwise.
    """
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError(f"backend must be one of {'|'.join(BACKENDS)}")
    return backend
