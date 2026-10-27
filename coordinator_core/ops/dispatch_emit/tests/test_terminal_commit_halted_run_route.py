"""dispatch.terminal_commit names the landed route for a halted run on its script_path and unreviewed refusals."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, terminal_commit
from coordinator_core.ops.dispatch_emit.halted_run_route import route_suffix

RUN_ID = "ask-20261023T121013-e7c4ae"
ROUTE = "dispatch.record_halted_run_landed {run_id: " + RUN_ID


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _mark(repo, status="halted"):
    d = ask_contract.record_dir(repo, RUN_ID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "halted.json").write_text(
        json.dumps({"status": status, "halted": "ledger-check", "blocking": ["R1"]}), encoding="utf-8"
    )


def _digest(repo, **extra):
    path = repo / "out.json"
    params = {"run_id": RUN_ID, "note": "n", "ledger_verdict": {"ok": False}, **extra}
    path.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return str(path)


def _call(repo, params):
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_scriptless_digest_of_a_halted_run_refuses_with_the_route(repo):
    _mark(repo)
    out = _call(repo, {"task_output_path": _digest(repo)})
    assert out["refused"] == "halted-run"
    assert f"{RUN_ID} halted at ledger-check" in out["error"]
    assert ROUTE in out["error"]


@pytest.mark.parametrize("status", ["landed", "resumed"])
def test_non_halted_marker_keeps_the_generic_refusal(repo, status):
    _mark(repo, status)
    out = _call(repo, {"task_output_path": _digest(repo)})
    assert out.get("refused") != "halted-run"
    assert ROUTE not in out["error"]


def test_no_marker_keeps_the_generic_refusal(repo):
    out = _call(repo, {"task_output_path": _digest(repo)})
    assert out.get("refused") != "halted-run"
    assert ROUTE not in out["error"]


def test_script_path_present_does_not_take_the_halted_arm(repo):
    _mark(repo)
    out = _call(repo, {"task_output_path": _digest(repo, script_path="scratch/warp/x.mjs")})
    assert out.get("refused") != "halted-run"


def test_route_suffix_only_for_a_halted_run(repo):
    assert route_suffix(repo, RUN_ID) == ""
    assert route_suffix(repo, None) == ""
    _mark(repo)
    assert ROUTE in route_suffix(repo, RUN_ID)
