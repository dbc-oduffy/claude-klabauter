"""A spool entry whose run stays at `minted` past the dispatch deadline is halted and its submitter noticed."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.ask_spool.tests.test_drain import Engine, _notices, _spool
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, write_run_stage, write_run_stage_at

LATE = drain.MINTED_DISPATCH_DEADLINE_SECONDS + 60


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


def _minted_entry(root, stage="minted"):
    sid = _spool(root)
    drain.run(str(root), evaluate=Engine(root))
    header, _ = record.read_entry(record.entry_path(str(root), sid, record.MINTED_SUFFIX))
    run_id = header["run_id"]
    write_run_stage(root, run_id, stage)
    return sid, run_id


def _marker(root, run_id):
    path = root / RECORD_DIR_ROOT / run_id / "halted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _halts(root):
    return [n for n in _notices(root) if n.get("status") == "halted"]


def test_minted_past_the_deadline_is_halted_with_reason_and_noticed(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    marker = _marker(tmp_path, run_id)
    assert marker["status"] == "halted" and marker["halted"] == "minted_without_dispatch"
    assert "never dispatched" in marker["reason"] and "workflow" in marker["reason"]
    (halt,) = _halts(tmp_path)
    assert halt["run_id"] == run_id and halt["halted"] == "minted_without_dispatch" and halt["reason"] == marker["reason"]


def test_a_fresh_entry_is_untouched(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + 10)
    assert _marker(tmp_path, run_id) is None
    assert _halts(tmp_path) == []


def test_a_run_past_minted_is_untouched(tmp_path):
    _, run_id = _minted_entry(tmp_path, stage="staged")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id) is None
    assert _halts(tmp_path) == []


def test_a_second_drain_does_not_halt_again(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    drain.run(str(tmp_path), now=time.time() + LATE)
    first = _marker(tmp_path, run_id)
    drain.run(str(tmp_path), now=time.time() + LATE + 60)
    assert _marker(tmp_path, run_id) == first
    assert len(_halts(tmp_path)) == 1


def test_a_launcher_parent_with_an_active_chained_child_is_untouched(tmp_path):
    _, parent = _minted_entry(tmp_path)
    now = time.time() + LATE
    recent = now - 30
    child = "warp-child-20261115T203651-abcdef"
    child_dir = tmp_path / RECORD_DIR_ROOT / child
    child_dir.mkdir(parents=True)
    (child_dir / "run.json").write_text(json.dumps({"chain_parent": parent}), encoding="utf-8")
    stamp = datetime.fromtimestamp(recent, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_run_stage_at(child_dir, "staged", now=stamp)
    for path in (*child_dir.iterdir(), child_dir):
        os.utime(path, (recent, recent))
    drain.run(str(tmp_path), now=now)
    assert _marker(tmp_path, parent) is None
    assert _halts(tmp_path) == []


def test_a_silent_chained_child_does_not_save_the_parent(tmp_path):
    _, parent = _minted_entry(tmp_path)
    child = "warp-child-20261115T203651-abcdef"
    child_dir = tmp_path / RECORD_DIR_ROOT / child
    child_dir.mkdir(parents=True)
    (child_dir / "run.json").write_text(json.dumps({"chain_parent": parent}), encoding="utf-8")
    write_run_stage(tmp_path, child, "staged")
    drain.run(str(tmp_path), now=time.time() + 4 * 3600)
    assert _marker(tmp_path, parent)["halted"] == "minted_without_dispatch"


def test_an_already_halted_run_is_not_rewritten(tmp_path):
    _, run_id = _minted_entry(tmp_path)
    halted_run.write_halt_marker(tmp_path / RECORD_DIR_ROOT / run_id, halted="other", sizing_rel=None, reason="prior")
    drain.run(str(tmp_path), now=time.time() + LATE)
    assert _marker(tmp_path, run_id)["halted"] == "other"
    assert _halts(tmp_path) == []
