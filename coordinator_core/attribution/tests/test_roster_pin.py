"""Pins the generated persona roster against coordinator-content-repo's live agent files —
a silent guard against roster drift. Resolves the content checkout through
`coordinator_core.testing.content_root`, never `claude_machine_local` (which does
not exist and made this test skip on every box). Runtime code must never
read coordinator-content-repo's tree; this test is the one sanctioned reader.
"""
from pathlib import Path

import pytest

from coordinator_core.attribution.roster import PERSONA_NAMES, derive_persona_names
from coordinator_core.testing.content_root import resolve_content_root


def _find_doe_agents_dir():
    root = resolve_content_root()
    if not root:
        return None
    agents_dir = Path(root) / "coordinator" / "agents"
    return agents_dir if agents_dir.is_dir() else None


@pytest.mark.real_home
def test_roster_matches_doe_agents_when_present():
    agents_dir = _find_doe_agents_dir()
    if agents_dir is None:
        pytest.skip("coordinator-content-repo checkout not reachable from this environment")

    agent_texts = [
        p.read_text(encoding="utf-8") for p in sorted(agents_dir.glob("*.md"))
    ]
    derived = derive_persona_names(agent_texts)
    missing = [name for name in derived if name not in PERSONA_NAMES]
    assert not missing, (
        "PERSONA_NAMES is missing derived persona name(s) "
        f"{missing} — update roster.py. Full derived tuple: {derived}"
    )
