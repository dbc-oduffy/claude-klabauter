"""seam_fix's collision pass reads out-of-set plans through _outside_plan, which
needs ctx["edge_cache"]; a ctx missing it crashed the stamping mise-prep run."""
from pathlib import Path

from coordinator_core.ops import plan_seam_fix


def test_collision_pairs_ctx_carries_edge_cache(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_findings(pset, ctx):
        seen.update(ctx)
        return []

    monkeypatch.setattr(plan_seam_fix, "_collision_findings", fake_findings)
    assert plan_seam_fix._collision_pairs({}, set(), tmp_path) == []
    assert "edge_cache" in seen and "outside" in seen
