"""
coordinator_core.session_baton — the lazy, session-scoped baton store.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C1 (D-A).

This package holds ONLY the store primitive (``store.py``). Minting (C2) and
promotion into ``state/handoffs/`` (C3) are separate ops layered on top and
are deliberately NOT implemented here — see ``store.py``'s module docstring
for the HARD CONSTRAINT this chunk is scoped by.
"""

from __future__ import annotations
