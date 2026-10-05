"""Refusal of receipt extras that would shadow the receipt's own core keys."""

from __future__ import annotations

from typing import Optional

RECEIPT_CORE_KEYS = frozenset({"sha256", "session_id", "emitted_at", "plan"})


def refuse_colliding_receipt_extras(receipt_extras: Optional[dict]) -> None:
    """Raise ValueError when ``receipt_extras`` names a key the wrapper always
    writes. Called before anything reaches disk so a refused emission leaves
    no partial script behind."""
    if not receipt_extras:
        return
    colliding = sorted(set(receipt_extras) & RECEIPT_CORE_KEYS)
    if colliding:
        raise ValueError(
            f"queue emission receipt_extras collide with wrapper-written "
            f"receipt keys {colliding}; extras may add new keys only "
            "(sha256/session_id/emitted_at/plan belong to the wrapper)."
        )
