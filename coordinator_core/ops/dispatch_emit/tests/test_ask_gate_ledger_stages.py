"""dispatch.ask_gate on the ledger-resume path stages the resumed plan and returns the whole manifest."""

from __future__ import annotations

import json

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_UNLANDED_FIELD
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
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
    assert "manifest_handle" in out and "error" not in out["manifest"], out  # interim: inline beside the handle
    manifest = read_reply(halted, out["manifest_handle"])
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

    assert "manifest_handle" in out and "error" not in out["manifest"], out  # interim: inline beside the handle
    assert read_reply(halted, out["manifest_handle"])["rows"] is not None
    assert len(staged) == len(bare), [s.argv for s in staged]


def test_a_resume_whose_halt_landed_every_staged_row_has_no_unlanded_rows(halted):  # noqa: F811
    _met_out_of_band(halted)

    out = _handler({"sizing_path": SIZING, "run_id": "newrun", "writes": ["a.py"]}, repo_root=halted)

    assert out["resume_ledger"][RESUME_LEDGER_UNLANDED_FIELD] == [], out["resume_ledger"]
    assert "manifest_handle" in out and "error" not in out["manifest"], out  # interim: inline beside the handle
    assert "manifest_handle" in out


def test_a_halt_that_landed_no_chunk_names_its_staged_rows_unlanded_and_stages_exactly_them(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
        HALTED_RUN,
        REFIRE_RUN,
        REGISTER,
        REMAINING,
        _git,
        _plan,
        _put,
        REL,
        PLAN_REL,
        ACCEPTED,
    )
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C0", "open"), ("C1", "open")])
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    for rel in ("some/C0.py", "some/C1.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", **REMAINING["R1"]}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C0", "writes": ["some/C0.py"]}, {"id": "C1", "writes": ["some/C1.py"]}]}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL}))

    out = _handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo_root=repo)

    assert out.get("halt") is None, out
    assert sorted(out["resume_ledger"][RESUME_LEDGER_UNLANDED_FIELD]) == ["C0", "C1"], out["resume_ledger"]
    manifest = read_reply(repo, out["manifest_handle"])
    assert sorted(r["id"] for r in manifest["rows"]) == ["C0", "C1"], manifest
