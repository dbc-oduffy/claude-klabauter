"""L routes plan by default; only a mechanically-uniform flag with a reason routes it spec-dispatch."""

from pathlib import Path

import pytest
import yaml

from coordinator_core import sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes
from coordinator_core.sizing_route_table import _BASE_ROUTE_BY_TSHIRT

_L = {"tshirt": "L"}


def test_route_table_l_is_plan():
    assert _BASE_ROUTE_BY_TSHIRT["L"] == "plan"


def test_l_without_flag_routes_plan():
    decision = sa.route(estimate=_L)
    assert decision["route"] == "plan"
    assert decision["mechanically_uniform"] is None


def test_l_with_flag_and_reason_routes_spec_dispatch():
    decision = sa.route(
        estimate=_L,
        mechanically_uniform=True,
        mechanically_uniform_reason="same edit across 12 identical call sites",
    )
    assert decision["route"] == "spec-dispatch"
    assert decision["mechanically_uniform"] == "same edit across 12 identical call sites"


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_l_with_flag_without_reason_is_refused(reason):
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate=_L, mechanically_uniform=True, mechanically_uniform_reason=reason)


def test_reason_without_flag_is_refused():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate=_L, mechanically_uniform_reason="uniform")


def test_flag_does_not_move_a_non_l_route():
    decision = sa.route(
        estimate={"tshirt": "M"}, mechanically_uniform=True, mechanically_uniform_reason="r"
    )
    assert decision["route"] == "plan"
    assert decision["mechanically_uniform"] is None


def test_flag_on_a_shape_routed_l_records_no_reason():
    decision = sa.route(
        estimate=_L,
        jtbd_unclear=True,
        mechanically_uniform=True,
        mechanically_uniform_reason="uniform",
    )
    assert decision["route"] == "shape"
    assert decision["mechanically_uniform"] is None


def test_cli_flag_with_reason_routes_l_spec_dispatch(capsys):
    argv = [
        "--appetite", "large", "--tshirt", "L", "--mechanically-uniform",
        "--mechanically-uniform-reason", "same edit at every site",
    ]
    assert sa.main(argv) == sa.EXIT_OK
    out = capsys.readouterr().out
    assert '"route": "spec-dispatch"' in out
    assert '"mechanically_uniform": "same edit at every site"' in out


def test_written_yaml_carries_the_reason_and_validates(tmp_path):
    (tmp_path / ".git").mkdir()
    path = Path(record_homes.record_path(str(tmp_path), "sizings", "2026-10-27-x.yaml"))
    path.parent.mkdir(parents=True)
    path.write_text(
        'schema: sizing-object\nintent: "Ship it"\nestimate:\n  tshirt: L\n  provisional: true\n'
        "route: plan\ndetents: []\nfork: null\nxl_exit: null\nstatus: draft\npremise:\n"
        "  provenance: unrecorded\n  evidence: PLACEHOLDER - cite the evidence\ndeliverable_id: null\n",
        encoding="utf-8",
    )
    decision = sa.route(
        estimate=_L, intent="Ship it", mechanically_uniform=True, mechanically_uniform_reason="uniform"
    )
    sa.write_back(tmp_path, str(path), decision)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    assert doc["mechanically_uniform"] == "uniform"
    assert validate_frontmatter(doc, str(schema)) == []
