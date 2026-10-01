import pytest


@pytest.fixture(autouse=True)
def _no_ambient_plugin_root(monkeypatch):
    """Fixture rosters name agent types no real plugin ships; an ambient
    CLAUDE_PLUGIN_ROOT from the invoking session would arm the emit-time
    agent-type resolution against them. Tests that exercise the check set it."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


_V5_ROSTER_FRAGMENT = {
    "schema": "review-roster-fragment",
    "schema_version": 5,
    "execute_review": {
        "stages": [
            {
                "kind": "prep",
                "agents": [
                    {
                        "agentType": "coordinator:review-prep",
                        "model": "sonnet",
                        "effort": "low",
                        "schema": "prep",
                    }
                ],
            },
            {
                "kind": "review-wave",
                "agents": [
                    {
                        "agentType": "coordinator:code-reviewer",
                        "model": "opus",
                        "effort": "low",
                        "per": "whole-diff",
                        "schema": "wave",
                    }
                ],
            },
            {
                "kind": "integration",
                "agents": [
                    {
                        "agentType": "coordinator:integrator",
                        "model": "opus",
                        "effort": "low",
                        "schema": "integration",
                    }
                ],
            },
        ]
    },
}

_V5_STAGE_SCHEMAS = {
    "prep": {"type": "object"},
    "wave": {"type": "object"},
    "integration": {"type": "object"},
}

#: Keyword arguments every compose_script/emit_script call needs: an emitted
#: workflow always carries the review wave, so no caller may omit them.
REVIEW_KW = {
    "review_roster_fragment": _V5_ROSTER_FRAGMENT,
    "review_stage_schemas": _V5_STAGE_SCHEMAS,
}


def execute_section(script: str) -> str:
    """The emitted script up to its review wave, for assertions about the
    execute phase that the review wave's own agents would otherwise satisfy."""
    return script.split("phase('Review prep')", 1)[0]
