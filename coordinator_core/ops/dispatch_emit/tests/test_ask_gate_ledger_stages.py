"""dispatch.ask_gate on the ledger-resume path stages the resumed plan and returns the whole manifest."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    PLAN,
    SIZING,
    _met_out_of_band,
    halted,
    repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def test_ledger_resume_verdict_carries_a_whole_manifest(halted):  # noqa: F811
    _met_out_of_band(halted)

    out = _handler({"sizing_path": SIZING, "run_id": "newrun", "writes": ["a.py"]}, repo_root=halted)

    assert out["resume_ledger"]["plan"] == PLAN, out
    manifest = out["manifest"]
    assert "error" not in manifest, manifest
    assert manifest["run_dir"] and manifest["marker_path"] and "rows" in manifest
    assert "head_sha" in manifest


def test_ledger_resume_without_run_id_carries_no_manifest(halted):  # noqa: F811
    _met_out_of_band(halted)

    out = _handler({"sizing_path": SIZING}, repo_root=halted)

    assert out["resume_ledger"] is not None and "manifest" not in out


def test_non_ledger_verdict_has_no_manifest_key(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put

    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _put(tmp_path, tshirt="S", route="spec-dispatch")

    out = _handler({"sizing_path": REL, "run_id": "r1"}, repo_root=tmp_path)

    assert "manifest" not in out


def test_staging_adds_no_spawn_to_the_ledger_resume_gate(halted, monkeypatch):  # noqa: F811
    _met_out_of_band(halted)

    with _count_spawns_attributed(monkeypatch) as bare:
        _handler({"sizing_path": SIZING}, repo_root=halted)
    with _count_spawns_attributed(monkeypatch) as staged:
        out = _handler({"sizing_path": SIZING, "run_id": "newrun", "writes": ["a.py"]}, repo_root=halted)

    assert "error" not in out["manifest"], out["manifest"]
    assert len(staged) == len(bare), [s.argv for s in staged]
