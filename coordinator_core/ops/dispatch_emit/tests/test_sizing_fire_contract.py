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
    assert sf.resolve_arm(_sizing(tshirt, route=sf.ARM_ROUTE[arm])) == arm


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
    s = _sizing("XL", route="plan", interaction_mode=None, exit_criterion={})
    out = sf.collect_fire_refusals(s, sizing_rel="state/sizings/a.yaml", arm="m_plus", writes=[])
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
    assert sf.s_plan_path("state/sizings/2026-10-01-foo.yaml") == "docs/plans/2026-10/2026-10-01-foo.md"


def test_existing_s_plan_resumes_instead_of_refusing(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "a.md").write_text("x", encoding="utf-8")
    sink: list = []
    out = sf.collect_fire_refusals(
        _sizing(), sizing_rel="state/sizings/a.yaml", arm="s", writes=[], repo_root=tmp_path, resume_sink=sink
    )
    assert out == [] and [p.plan_rel for p in sink] == ["docs/plans/a.md"]


def test_s_arm_without_a_plan_on_disk_resumes_nothing(tmp_path):
    sink: list = []
    out = sf.collect_fire_refusals(
        _sizing(), sizing_rel="state/sizings/a.yaml", arm="s", writes=[], repo_root=tmp_path, resume_sink=sink
    )
    assert out == [] and sink == [None]


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


def _write_sizing(root, **over):
    d = root / "state" / "sizings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.yaml").write_text(yaml.safe_dump(_sizing(**over)), encoding="utf-8")


def test_sizing_memo_parses_once_per_scope_and_hands_out_independent_copies(tmp_path, monkeypatch):
    _write_sizing(tmp_path)
    parses: list = []
    real = sf.yaml.safe_load
    monkeypatch.setattr(sf.yaml, "safe_load", lambda text: parses.append(1) or real(text))
    with sf.sizing_memo():
        first = sf.load_sizing(tmp_path, "state/sizings/a.yaml")
        first["route"] = "mutated"
        second = sf.load_sizing(tmp_path, "state/sizings/a.yaml")
    assert len(parses) == 1 and second["route"] == "spec-dispatch"


def test_sizing_memo_does_not_outlive_its_scope_and_sees_a_rewrite_between_calls(tmp_path):
    _write_sizing(tmp_path, route="spec-dispatch")
    with sf.sizing_memo():
        assert sf.load_sizing(tmp_path, "state/sizings/a.yaml")["route"] == "spec-dispatch"
    _write_sizing(tmp_path, route="plan")
    assert sf.load_sizing(tmp_path, "state/sizings/a.yaml")["route"] == "plan"
    with sf.sizing_memo():
        assert sf.load_sizing(tmp_path, "state/sizings/a.yaml")["route"] == "plan"


def test_sizing_memo_rewrite_inside_a_scope_is_read_when_the_stat_key_moves(tmp_path):
    _write_sizing(tmp_path, route="spec-dispatch")
    with sf.sizing_memo():
        sf.load_sizing(tmp_path, "state/sizings/a.yaml")
        # A longer route changes the file size, which moves the memo's stat key even on a coarse-mtime filesystem.
        _write_sizing(tmp_path, route="pm-decision-xxxxxxxx")
        assert sf.load_sizing(tmp_path, "state/sizings/a.yaml")["route"] == "pm-decision-xxxxxxxx"


def test_sizing_memo_still_refuses_a_missing_or_escaping_sizing(tmp_path):
    with sf.sizing_memo():
        with pytest.raises(sf.SizingFireRefused):
            sf.load_sizing(tmp_path, "state/sizings/none.yaml")
        with pytest.raises(sf.SizingFireRefused):
            sf.load_sizing(tmp_path, "state/sizings/../../outside.yaml")


@pytest.mark.parametrize(
    "tshirt,route,asked",
    [("L", "spec-dispatch", False), ("M", "dispatch", False), ("XL", "plan", True), ("S", "shape", True), ("M", "pm-decision", True)],
)
def test_null_acceptance_is_refused_only_outside_the_engine_size_rule(tshirt, route, asked):
    s = _sizing(tshirt, route=route, exit_criterion={"statement": "x", "accepted": None})
    out = sf.collect_fire_refusals(s, sizing_rel="state/sizings/a.yaml", arm="m_plus", writes=[])
    assert any(r.startswith("`exit_criterion.accepted`") for r in out) is asked
