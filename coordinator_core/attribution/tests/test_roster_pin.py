"""Pins the generated persona roster against DoE-claude's agent files when
that checkout is reachable — a silent guard against roster drift.
"""
import re

import pytest

from coordinator_core.attribution.roster import PERSONA_NAMES


def _find_doe_agents_dir():
    try:
        from claude_machine_local import repos
    except ImportError:
        return None
    agents_dir = repos.doe_claude / "coordinator/agents"
    return agents_dir if agents_dir.is_dir() else None


def test_roster_matches_doe_agents_when_present():
    agents_dir = _find_doe_agents_dir()
    if agents_dir is None:
        pytest.skip("DoE-claude checkout not reachable from this environment")

    text = "\n".join(p.read_text(encoding="utf-8") for p in agents_dir.glob("*.md"))
    known_core = {"the Staff Engineer", "the Data Science Reviewer", "Kira", "Angelique", "the Game Dev Reviewer", "the VP-Product Reviewer", "the UX Reviewer"}
    for name in known_core:
        assert re.search(rf"\b{re.escape(name)}\b", text), (
            f"{name} no longer found in DoE agent roster — update PERSONA_NAMES"
        )
    for name in known_core:
        assert name in PERSONA_NAMES
