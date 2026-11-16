"""A single engine-ask's chain emit mints its own run id; the chained script never carries the ask's.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_single_ask_fresh_run_id.py -q
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import sizing_intake_contract as contract
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from pathlib import Path

_SIZING = "state/sizings/2026-11-14-slice.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_ASK_TEXT = "plan the slice"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _ask_run_with_sized_slice(repo) -> str:
    """A raw single ask minted, then bound to the sizing its in-run size stage wrote."""
    reply = op._dispatch_emit({"ask": _ASK_TEXT}, repo_root=repo)
    ask_id = reply["run_id"]
    doc = contract.intake_record(name="slice", turn={"text": _ASK_TEXT, "turn": 1}, today="2026-11-14")
    (repo / _SIZING).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.bind(repo, ask_id, _SIZING, ask_text=_ASK_TEXT)
    return ask_id


def _chain_emit(repo, ask_id: str) -> dict:
    return op._dispatch_emit(
        {"ask": True, "sizing_path": _SIZING, "target_root": str(repo), "chain_parent": ask_id, "launcher_child": True}
    )


def test_a_single_ask_chain_emit_names_a_fresh_run_not_the_asks_own(repo) -> None:
    ask_id = _ask_run_with_sized_slice(repo)
    reply = _chain_emit(repo, ask_id)
    assert "error" not in reply
    assert reply["run_id"] != ask_id
    assert not str(reply["path"]).endswith(f"{ask_id}.workflow.mjs")
    assert (repo / RECORD_DIR_ROOT / reply["run_id"]).is_dir()


def test_a_halted_ask_run_is_not_rebound_by_its_own_chain_emit(repo) -> None:
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    ask_id = _ask_run_with_sized_slice(repo)
    halted_run.write_halt_marker(record_dir(repo, ask_id), halted="x", sizing_rel=_SIZING)
    assert _chain_emit(repo, ask_id)["run_id"] != ask_id


def test_a_raw_chain_emit_naming_its_parent_as_the_preminted_id_still_mints_fresh(repo) -> None:
    ask_id = op._dispatch_emit({"ask": _ASK_TEXT}, repo_root=repo)["run_id"]
    reply = op._dispatch_emit(
        {"ask": "another raw ask", "target_root": str(repo), "chain_parent": ask_id, "preminted_run_id": ask_id}
    )
    assert reply["run_id"] != ask_id
    assert not str(reply["path"]).endswith(f"{ask_id}.workflow.mjs")


def test_a_plain_raw_emit_still_honours_its_preminted_id(repo) -> None:
    pre = "ask-20261114T220832-5d30b8"
    assert op._dispatch_emit({"ask": _ASK_TEXT, "preminted_run_id": pre}, repo_root=repo)["run_id"] == pre
