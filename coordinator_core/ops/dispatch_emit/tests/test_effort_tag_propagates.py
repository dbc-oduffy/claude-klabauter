"""A spine row's effort tag rides read_spine, build_waves and route_rows unchanged."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import agent_route
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

_PLAN = (
    "---\ntitle: fixture\n---\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: A1\n  title: hot\n  effort: high\n  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n"
    "- id: A2\n  title: plain\n  surface: pkg/b.py\n  writes:\n    - pkg/b.py\n```\n"
)


def _spine(tmp_path):
    (tmp_path / ".git").mkdir()
    plan = tmp_path / "docs" / "plans" / "2026-10-25-effort.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(_PLAN, encoding="utf-8", newline="\n")
    return read_spine(plan, schema_preflight=False)


def test_effort_survives_spine_waves_and_unrouted_route(tmp_path):
    rows = _spine(tmp_path)
    assert {r.id: r.effort for r in rows} == {"A1": "high", "A2": None}

    flat = [r for wave in build_waves(rows) for r in wave]
    assert {r.id: r.effort for r in flat} == {"A1": "high", "A2": None}

    (tmp_path / ".claude").mkdir()
    table = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "never",
                "match": {"paths": ["nomatch/*"]},
                "agent_type": "coordinator:executor",
                "required_tools": [],
                "on_mismatch": "refuse",
            }
        ],
    }
    (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    routed, _, _ = agent_route.route_rows(flat, tmp_path)
    assert {r.id: r.effort for r in routed} == {"A1": "high", "A2": None}
