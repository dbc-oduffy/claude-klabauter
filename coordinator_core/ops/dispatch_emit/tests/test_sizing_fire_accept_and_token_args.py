"""_gate_sizing_at_emit admits XS-L sizing acceptance in any mode; the run token rides fire_args only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused
from coordinator_core.session.record_homes import record_path

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


def _write_sizing(root: Path, name: str, *, tshirt: str, route: str, mode: str) -> str:
    rel = record_path("", "sizings", name)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "intent": "Fix the friction",
                "estimate": {"tshirt": tshirt},
                "route": route,
                "status": "sized",
                "interaction_mode": mode,
                "premise": {"provenance": "executed", "evidence": "ran the gate, saw the halt"},
                "exit_criterion": {"statement": None, "accepted": None},
            }
        ),
        encoding="utf-8",
    )
    return rel


def test_hands_on_s_without_statement_is_admitted_pending_acceptance(tmp_path):
    rel = _write_sizing(tmp_path, "s.yaml", tshirt="S", route="spec-dispatch", mode="hands-on")
    out = _gate_sizing_at_emit(tmp_path, rel, ["a.py"])
    assert out.get("accept_pending") is True


def test_hands_on_xl_without_statement_still_refused(tmp_path):
    rel = _write_sizing(tmp_path, "xl.yaml", tshirt="XL", route="plan", mode="hands-on")
    with pytest.raises(SizingFireRefused):
        _gate_sizing_at_emit(tmp_path, rel, ["a.py"])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def test_fire_args_carries_run_token_and_stdout_record_does_not(repo, capsys):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

    result = _dispatch_emit({"ask": "x", "repo_root": str(repo)}, repo_root=repo)
    token = result["fire_args"].get("runToken")
    assert token

    rc = cli.main(["--ask", "x", "--repo-root", str(repo)])
    cap = capsys.readouterr()
    assert rc == 0
    assert "runToken" not in json.loads(cap.out).get("fire_args", {})
    assert "runToken" in cap.err
