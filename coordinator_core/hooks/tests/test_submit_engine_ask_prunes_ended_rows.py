"""The engine-ask overlap guard treats resumed, terminal and dir-less index rows as not live and drops them."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import live_run_index

TOOL_INPUT = {"args": {"writes": ["pkg/mod.py"]}}


def _row(root: Path, run_id: str) -> None:
    live_run_index.stage(root, run_id, {"pkg/mod.py"}, minted_at=1.0)


def _marker(root: Path, run_id: str, status: str) -> None:
    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "halted.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def _held(root: Path) -> set:
    index = live_run_index.read(root, 500.0)
    return set(index) if isinstance(index, dict) else set()


def test_resumed_and_dirless_rows_deny_nothing_and_leave_the_index(tmp_path):
    (tmp_path / ".git").mkdir()
    _row(tmp_path, "run-resumed")
    _row(tmp_path, "run-ghost")
    _marker(tmp_path, "run-resumed", "resumed")
    assert mod._live_write_overlap_deny(tmp_path, TOOL_INPUT, {}) is None
    assert not (_held(tmp_path) & {"run-resumed", "run-ghost"})


def test_superseded_landed_and_closed_rows_are_dropped(tmp_path):
    (tmp_path / ".git").mkdir()
    for status in ("superseded", "landed", "closed"):
        run_id = f"run-{status}"
        _row(tmp_path, run_id)
        _marker(tmp_path, run_id, status)
    assert mod._live_write_overlap_deny(tmp_path, TOOL_INPUT, {}) is None
    assert not _held(tmp_path)


def test_a_live_run_still_refuses_and_keeps_its_row(tmp_path):
    (tmp_path / ".git").mkdir()
    _row(tmp_path, "run-live")
    rd = tmp_path / "state" / "warp" / "run-live"
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    why = mod._live_write_overlap_deny(tmp_path, TOOL_INPUT, {})
    assert why and "run-live" in why
    assert "run-live" in _held(tmp_path)


def test_a_halted_run_denies_nothing_but_keeps_its_resumable_row(tmp_path):
    (tmp_path / ".git").mkdir()
    _row(tmp_path, "run-halted")
    _marker(tmp_path, "run-halted", "halted")
    assert mod._live_write_overlap_deny(tmp_path, TOOL_INPUT, {}) is None
    assert "run-halted" in _held(tmp_path)
