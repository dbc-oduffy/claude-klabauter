"""route() refuses a scope whose implied composed script is past the Workflow byte cap, naming split."""

import pytest

from coordinator_core import sizing_assemble as sa
from coordinator_core.ops.dispatch_emit import emit


def test_estimator_is_calibrated_on_the_a775c4_case():
    assert 1822 * sa.SCRIPT_BYTES_PER_SCOPE_FILE > emit._WORKFLOW_SCRIPT_BYTE_CAP
    assert abs(1822 * sa.SCRIPT_BYTES_PER_SCOPE_FILE - 961587) < 1000


def test_a775c4_scope_is_refused_naming_split():
    with pytest.raises(sa.SizingAssembleError, match="(?i)split"):
        sa.route(estimate={"tshirt": "L"}, scope_files=1822, scope_lines=7689)


def test_just_over_the_cap_is_refused():
    over = emit._WORKFLOW_SCRIPT_BYTE_CAP // sa.SCRIPT_BYTES_PER_SCOPE_FILE + 1
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate={"tshirt": "L"}, scope_files=over)


def test_at_the_cap_still_routes():
    at = emit._WORKFLOW_SCRIPT_BYTE_CAP // sa.SCRIPT_BYTES_PER_SCOPE_FILE
    assert sa.route(estimate={"tshirt": "S"}, scope_files=at)["resolved_estimate"]["tshirt"] == "L"


def test_ordinary_l_scope_and_absent_scope_are_not_refused():
    assert sa.route(estimate={"tshirt": "S"}, scope_files=7, scope_lines=1643)["resolved_estimate"]["tshirt"] == "L"
    assert sa.route(estimate={"tshirt": "S"})["resolved_estimate"]["tshirt"] == "S"


def test_cli_refusal_exits_usage_naming_split(capsys):
    rc = sa.main(["--tshirt", "L", "--scope-files", "1822"])
    assert rc == sa.EXIT_USAGE
    assert "split" in capsys.readouterr().err.lower()
