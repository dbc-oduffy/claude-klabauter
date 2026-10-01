"""The ask seam end to end through `_dispatch_emit` with the real composer over a tmp_path repo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_PHASES, OP_ASK_GATE, OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _stub_wrap(text):
    return _BLITZ_FN, ["Size", "Plan"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.setattr(op, "_load_review_inputs", lambda route: tuple(REVIEW_KW.values()))
    monkeypatch.setattr(op, "check_agent_types_resolve", lambda *a, **k: None)
    from coordinator_core.ops.dispatch_emit import ask_plan_blitz

    monkeypatch.setattr(ask_plan_blitz, "wrap_stage", _stub_wrap)
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.ask_compose._read_plan_blitz", lambda: "stub"
    )
    return tmp_path


def _titles(script: str) -> list[str]:
    block = script.split("phases: [", 1)[1].split("]", 1)[0]
    return re.findall(r"'([^']*)'", block)


def test_raw_ask_emits_the_full_phase_chain_with_both_verbs(repo):
    reply = op._dispatch_emit({"ask": "add the widget"}, repo_root=repo)
    text = Path(reply["path"]).read_text(encoding="utf-8")
    titles = _titles(text)
    positions = [titles.index(p) for p in ASK_PHASES]
    assert positions == sorted(positions)
    assert OP_ASK_GATE in text and OP_ASK_STAGE in text
    assert reply["ok"], reply["findings"]


def test_sizing_entry_omits_the_size_phase(repo):
    rel = "state/sizings/s-job.yaml"
    (repo / rel).write_text(
        yaml.safe_dump({"estimate": {"tshirt": "S"}, "route": "spec-dispatch"}), encoding="utf-8"
    )
    reply = op._dispatch_emit({"ask": True, "sizing_path": rel}, repo_root=repo)
    text = Path(reply["path"]).read_text(encoding="utf-8")
    assert "phase('size')" not in text
    assert rel in text
