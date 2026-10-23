"""Test-only adapter. No non-test module imports this."""

from __future__ import annotations

from typing import Optional

from coordinator_core.guard_messages.alternative_record import AlternativeRecord, Audience
from coordinator_core.guard_messages.render import render


def envelope_of(result, audience: str = "em") -> Optional[dict]:
    """Render an AlternativeRecord; return a dict or None unchanged."""
    if isinstance(result, AlternativeRecord):
        return render(result, Audience(audience))
    return result
