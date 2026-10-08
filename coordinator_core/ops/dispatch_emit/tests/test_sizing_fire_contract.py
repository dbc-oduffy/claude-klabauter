"""Contract tests for sizing_fire: arm mapping, one-shot refusal collection, containment."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import sizing_fire as sf


def _sizing(tshirt="S", route="spec-dispatch", **over):
    doc = {
        "estimate": {"tshirt": tshirt},
        "route": route,
        "status": "routed",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes"}},
    }
    doc.update(over)
    return doc


@pytest.mark.parametrize(
    "tshirt,arm",
    [("XS", "xs"), ("S", "s"), ("M", "m_plus"), ("L", "m_plus"), ("XL", "m_plus"), ("XXL", "m_plus")],
)
def test_tshirt_maps_to_arm(tshirt, arm):
    assert sf.resolve_arm(_sizing(tshirt)) == arm


def test_unknown_tshirt_refuses():
    with pytest.raises(sf.SizingFireRefused):
        sf.resolve_arm(_sizing("Q"))


@pytest.mark.parametrize(
    "arm,route",
    [("xs", "spec-dispatch"), ("s", "dispatch"), ("m_plus", "goal-setting"), ("m_plus", "roadmap"), ("s", "pm-decision")],
)
def test_route_mismatch_refuses_by_name(arm, route):
    out = sf.collect_fire_refusals(_sizing(route=route), sizing_rel="state/sizings/a.yaml", arm=arm, writes=["x"])
    assert len(out) == 1 and "`route`" in out[0]


def test_matching_routes_are_fireable():
    for arm, route in sf.ARM_ROUTE.items():
        assert sf.collect_fire_refusals(
            _sizing(route=route), sizing_rel="state/sizings/a.yaml", arm=arm, writes=["x"]
        ) == []


def test_all_missing_fields_yield_one_refusal_list():
    s = _sizing(interaction_mode=None, exit_criterion={})
    out = sf.collect_fire_refusals(s, sizing_rel="state/sizings/a.yaml", arm="s", writes=[])
    joined = " | ".join(out)
    assert "statement" in joined and "accepted" in joined and "interaction_mode" in joined
    assert len(out) == 3
    assert "sizing-accept-exit-criterion --sizing state/sizings/a.yaml" in joined  # C2/C9: launcher hint


def test_xs_without_writes_refuses():
    out = sf.collect_fire_refusals(_sizing("XS", "dispatch"), sizing_rel="state/sizings/a.yaml", arm="xs", writes=[])
    assert len(out) == 1 and "`writes`" in out[0] and "--writes" not in out[0]


def test_shipped_status_refuses():
    out = sf.collect_fire_refusals(
        _sizing(status="shipped"), sizing_rel="state/sizings/a.yaml", arm="s", writes=[]
    )
    assert len(out) == 1 and "`status`" in out[0]


def test_s_plan_path():
    assert sf.s_plan_path("state/sizings/2026-10-01-foo.yaml") == "docs/plans/2026-10-01-foo.md"


def test_existing_s_plan_refuses_naming_plan_alternative(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "a.md").write_text("x", encoding="utf-8")
    out = sf.collect_fire_refusals(
        _sizing(), sizing_rel="state/sizings/a.yaml", arm="s", writes=[], repo_root=tmp_path
    )
    assert len(out) == 1 and "emit-dispatch-workflow --plan docs/plans/a.md" in out[0]


def test_load_sizing_contained(tmp_path):
    d = tmp_path / "state" / "sizings"
    d.mkdir(parents=True)
    (d / "a.yaml").write_text(yaml.safe_dump(_sizing()), encoding="utf-8")
    (tmp_path / "outside.yaml").write_text("a: 1", encoding="utf-8")
    assert sf.load_sizing(tmp_path, "state/sizings/a.yaml")["route"] == "spec-dispatch"
    with pytest.raises(sf.SizingFireRefused):
        sf.load_sizing(tmp_path, "outside.yaml")
    with pytest.raises(sf.SizingFireRefused):
        sf.load_sizing(tmp_path, "state/sizings/../../outside.yaml")
