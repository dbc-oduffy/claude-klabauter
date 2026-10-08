"""`sizing-assemble --click-path` writes `exit_criterion.click_paths`, nav entry first."""

import pytest

import coordinator_core.sizing_assemble as sa


def test_parse_click_path_splits_role_and_steps():
    assert sa.parse_click_path("admin: Settings > Users > Grant access") == {
        "role": "admin",
        "steps": ["Settings", "Users", "Grant access"],
    }


@pytest.mark.parametrize("raw", ["admin Settings > Grant", ": Settings > Grant", "admin: Settings"])
def test_malformed_click_path_is_refused(raw):
    with pytest.raises(sa.SizingAssembleError):
        sa.parse_click_path(raw)


def test_route_carries_click_paths_on_the_criterion():
    paths = [sa.parse_click_path("admin: Settings > Grant access")]
    out = sa.route(estimate={"tshirt": "M"}, exit_criterion="Admins can grant access.", click_paths=paths)
    assert out["exit_criterion"]["click_paths"] == paths


def test_click_path_without_a_criterion_is_refused():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate={"tshirt": "M"}, click_paths=[sa.parse_click_path("admin: A > B")])
