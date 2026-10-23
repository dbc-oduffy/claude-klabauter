"""dispatch.land_run names the landed route when a scriptless or unreviewed land targets a halted run."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.halted_run_route import route_line

RUN = "ask-20261023T121013-e7c4ae"


def _halt(root: Path, run_id: str, status: str = "halted") -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "halted.json").write_text(
        json.dumps({"status": status, "halted": "ledger-check", "sizing_rel": "state/sizings/x.yaml"}),
        encoding="utf-8",
    )


def _digest(tmp_path: Path, params: dict) -> str:
    path = tmp_path / "out.json"
    path.write_text(json.dumps({"result": {"next_action": {"params": params}}}), encoding="utf-8")
    return str(path)


def _land(root: Path, params: dict) -> dict:
    return land_run._handler(params, repo_root=root / ".git")


def test_scriptless_land_of_halted_run_names_route(tmp_path):
    _halt(tmp_path, RUN)
    out = _land(tmp_path, {"task_output_path": _digest(tmp_path, {"run_id": RUN, "ledger_verdict": "drifted"})})
    assert out["refused"] == "halted-run"
    assert f"{RUN} halted at ledger-check" in out["error"]
    assert route_line(RUN) in out["error"]


def test_scriptless_land_of_unhalted_run_keeps_generic_refusal(tmp_path):
    _halt(tmp_path, RUN, status="landed")
    out = _land(tmp_path, {"task_output_path": _digest(tmp_path, {"run_id": RUN})})
    assert out.get("refused") != "halted-run"
    assert "script_path" in out["error"]
