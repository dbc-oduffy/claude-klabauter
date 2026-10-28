"""Shared inline_review fixtures for landing tests: a review wave whose verdict resolves to a real sidecar.

Invariant: a test that pins the review floor's refusal uses ``unreviewed()`` and never ``reviewed()``;
every other landing test that wants a passing floor takes ``reviewed()``.
"""

from __future__ import annotations

from pathlib import Path

REVIEW_SIDECAR: str = str(Path(__file__).resolve().parent / "fixtures" / "review_sidecar.md")


def reviewed(**extra) -> dict:
    """A fresh inline_review carrying one PASS verdict whose absolute sidecar exists on disk; ``extra`` keys override the base."""
    return {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_reviews": [{"slice_id": "s1", "verdict": "PASS", "sidecar_path": REVIEW_SIDECAR}],
        **extra,
    }


def unreviewed() -> dict:
    """A fresh inline_review with no wave reviews; for floor-refusal tests only."""
    return {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
