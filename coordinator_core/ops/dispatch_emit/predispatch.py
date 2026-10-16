"""Agent spec shape for the pre-dispatch falsifier-integrity review phase.

Invariants: this module never imports ``emit`` at module level (``emit`` imports
it); a review prompt built from an ``AgentSpec`` never carries ``key``, which is
JS-side only.
"""

from __future__ import annotations

from typing import NamedTuple, Optional


class AgentSpec(NamedTuple):
    key: str
    label: str
    phase: str
    agent_type: Optional[str]
    model: str
    prompt: str
    schema: str
    #: Falsifier-integrity verdict cache. ``schema == CACHED_SCHEMA`` marks a hit:
    #: ``prompt`` is then the cached result JSON and no agent is dispatched. On a
    #: fresh review ``cache_path``/``cache_key`` tell the script where to persist
    #: the verdict it gets back.
    cache_path: Optional[str] = None
    cache_key: Optional[tuple[str, str]] = None


CACHED_SCHEMA = "cached_result"
