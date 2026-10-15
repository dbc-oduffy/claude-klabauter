"""sizing-assemble rewrites a suite-tier exit_criterion to the canonical phrase, or refuses naming it."""
from __future__ import annotations

import json

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.session import fleet_mode as fleet_mode_module
from coordinator_core.session import mode_resolution as mode_resolution_module

CANONICAL = "tests covering touched files pass"


def _fleet_stub(monkeypatch):
    fn = lambda: {}
    monkeypatch.setattr(fleet_mode_module, "read_fleet_mode", fn)
    monkeypatch.setattr(mode_resolution_module, "read_fleet_mode", fn)


@pytest.mark.parametrize(
    "text",
    [
        "the fast-tier tests for the touched files pass (python3 bin/run-fast-tests.py)",
        "the full suite tests on touched files pass",
    ],
)
def test_suite_tier_claim_is_rewritten(monkeypatch, capsys, text):
    _fleet_stub(monkeypatch)
    rc = sa.main(["--tshirt", "M", "--exit-criterion", text])
    assert rc == sa.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["exit_criterion"]["statement"] == CANONICAL


@pytest.mark.parametrize(
    "text",
    [
        "fast tier green",
        "tier-U",
        "the fast tier tests for touched files pass and the dashboard ships",
    ],
)
def test_unsafe_shape_is_refused_naming_the_rewrite(monkeypatch, capsys, text):
    _fleet_stub(monkeypatch)
    rc = sa.main(["--tshirt", "M", "--exit-criterion", text])
    assert rc == sa.EXIT_USAGE
    assert CANONICAL in capsys.readouterr().err
