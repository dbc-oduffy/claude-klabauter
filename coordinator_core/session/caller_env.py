"""Actual caller environment carried by an invocation, separate from daemon state.

This is cooperative caller metadata, never a credential or ordinary op params.
Returned mappings are copies; callers must not log bearer/delegation fields.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from typing import Mapping

_CARRIED: ContextVar[dict[str, str] | None] = ContextVar('coordinator_caller_env', default=None)


@contextmanager
def bind_caller_env(env: Mapping[str, str] | None):
    values = {key: value for key, value in (env or {}).items()
              if isinstance(key, str) and isinstance(value, str)}
    token = _CARRIED.set(values)
    try:
        yield
    finally:
        _CARRIED.reset(token)


def carried_caller_env() -> dict[str, str]:
    """Snapshot of explicitly carried fields; never reads process environment."""
    return dict(_CARRIED.get() or {})


def effective_caller_env() -> dict[str, str]:
    """Actual request fields when bound; own environment only for a cold caller."""
    from coordinator_core.session.core import in_warm_served_request

    values = _CARRIED.get()
    if values is not None or in_warm_served_request():
        return dict(values or {})
    return dict(os.environ)
