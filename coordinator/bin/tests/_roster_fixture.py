"""Shared helper: give a hand-built plugin root the roster fragment `emit-wave-fire` refuses without."""

from __future__ import annotations

import json
from pathlib import Path

ROSTER_BLOCK = {"tiers": {"M": {"reviewers": [{"agentType": "coordinator:staff-eng", "model": "opus"}]}}}


def write_roster_fragment(plugin_root: Path) -> None:
    """Write `<plugin_root>/contract/review-roster-fragment.json` with a non-empty `plan_review`."""
    contract = Path(plugin_root) / "contract"
    contract.mkdir(parents=True, exist_ok=True)
    (contract / "review-roster-fragment.json").write_text(
        json.dumps({"plan_review": ROSTER_BLOCK}), encoding="utf-8"
    )
