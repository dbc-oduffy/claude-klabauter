"""The chain emit for an ask-bound run mints a fresh `warp-` run id, never the ask's own.

Drives the production `dispatch.emit` entry point with the params the ask script's chain hop sends.
Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_chain_emit_never_reuses_ask_run_id.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, halted_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_stop_after_survives_chained_runs_e2e import (
    _BLITZ, _SIZING_DOC, REL,
)

SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_SIZING_DOC, sort_keys=False), encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SESSION)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    return tmp_path


def _ask_run(repo: Path) -> str:
    """A raw ask emitted under a premint, then bound to the sizing it minted: the chain hop's parent."""
    ask_id = run_token.new_ask_run_id()
    first = _dispatch_emit({"ask": "add a thing", "target_root": str(repo), "preminted_run_id": ask_id})
    assert Path(first["path"]).name == f"{ask_id}.workflow.mjs", first
    run_token.bind_sizing(repo, ask_id, REL)
    return ask_id


def _chain_emit(repo: Path, ask_id: str, **extra) -> dict:
    reply = _dispatch_emit({
        "ask": True, "sizing_path": REL, "session_id": SESSION, "target_root": str(repo),
        "chain_parent": ask_id, "launcher_child": True, **extra,
    })
    assert "error" not in reply, reply
    return reply


def _assert_fresh(reply: dict, ask_id: str) -> None:
    name = Path(reply["path"]).name
    assert name.startswith("warp-") and name != f"{ask_id}.workflow.mjs", reply


def test_chain_emit_mints_a_warp_run(repo):
    ask_id = _ask_run(repo)
    _assert_fresh(_chain_emit(repo, ask_id), ask_id)


def test_chain_emit_repeated_stays_fresh_each_time(repo):
    ask_id = _ask_run(repo)
    first, second = _chain_emit(repo, ask_id), _chain_emit(repo, ask_id)
    _assert_fresh(first, ask_id)
    _assert_fresh(second, ask_id)


def test_chain_emit_ignores_a_reserved_run_id_naming_the_ask(repo):
    """A reservation that names the ask run reuses its id at base; a chain hop takes none."""
    ask_id = _ask_run(repo)
    sizing = repo / REL
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    doc["em_analysis"] = {"ask_parent": f"parent {ask_id}"}
    sizing.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    _assert_fresh(_chain_emit(repo, ask_id, reserved_run_id=ask_id), ask_id)


def test_chain_emit_after_the_ask_halts_mints_a_warp_run(repo):
    ask_id = _ask_run(repo)
    halted_run.write_halt_marker(record_dir(repo, ask_id), halted="refusal", sizing_rel=REL)
    _assert_fresh(_chain_emit(repo, ask_id), ask_id)
