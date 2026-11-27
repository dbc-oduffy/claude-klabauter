"""Warm-engine residency marker: set once inside a warm pool worker, read by the door CLI.

No imports on purpose: a cold CLI reads it without paying for anything.
"""

_resident = False


def mark_resident() -> None:
    """Record that this process is a warm engine worker. Idempotent."""
    global _resident
    _resident = True


def is_resident() -> bool:
    """True only in a process that called `mark_resident`; a cold interpreter reads False."""
    return _resident
