"""Pins the generated persona roster against DoE-claude's live agent files —
a silent guard against roster drift. Resolves DoE through
`coordinator_core.doe_root_pointer`, never `claude_machine_local` (which does
not exist and made this test skip on every box). Runtime code must never
read DoE-claude's tree; this test is the one sanctioned reader.
"""
from pathlib import Path

import pytest

from coordinator_core.attribution.roster import PERSONA_NAMES, derive_persona_names
from coordinator_core.doe_root_pointer import read_doe_root_pointer


def _find_doe_agents_dir():
    root = read_doe_root_pointer()
    if not root:
        return None
    agents_dir = Path(root) / "coordinator" / "agents"
    return agents_dir if agents_dir.is_dir() else None


@pytest.mark.real_home
def test_roster_matches_doe_agents_when_present():
    agents_dir = _find_doe_agents_dir()
    if agents_dir is None:
        pytest.skip("DoE-claude checkout not reachable from this environment")

    agent_texts = [
        p.read_text(encoding="utf-8") for p in sorted(agents_dir.glob("*.md"))
    ]
    derived = derive_persona_names(agent_texts)
    missing = [name for name in derived if name not in PERSONA_NAMES]
    assert not missing, (
        "PERSONA_NAMES is missing derived persona name(s) "
        f"{missing} — update roster.py. Full derived tuple: {derived}"
    )
