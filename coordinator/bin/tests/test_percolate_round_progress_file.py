"""percolate-round seeds the publish child's --progress-file and arms a one-shot hang dump.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_progress_file.py -q
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("percolate_round_progress_under_test", _BIN_DIR / "percolate-round.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pr = _load()


def _drive_default_round(monkeypatch, tmp_path, run_side_effect):
    """Run `_cmd_round_default` to the end of Step 1 with a failing publish leg."""
    seen = {}

    def fake_run(cmd, *, timeout, **kwargs):
        seen["cmd"] = cmd
        run_side_effect(cmd, seen)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Rows FAILED: row-a")

    class _Slot:
        token = None
        repo_root = tmp_path

    pr._bootstrap_engine()
    monkeypatch.setattr(pr, "_round_held_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pr, "_hold_round_slot", lambda dest: contextlib.nullcontext(_Slot()))
    monkeypatch.setattr(pr, "publish_contention_wait_secs", lambda: 0)
    monkeypatch.setattr(pr, "_run", fake_run)
    args = argparse.Namespace(open_only=False, delta=True, source_ref=None)
    rc = pr._cmd_round_default(args, "row-a", str(tmp_path), str(tmp_path), str(tmp_path), tmp_path)
    return rc, seen


def test_progress_file_exists_before_the_publish_child_runs(monkeypatch, tmp_path):
    progress = tmp_path / "publish-progress.json"
    at_spawn = {}

    def side_effect(cmd, seen):
        idx = cmd.index("--progress-file")
        assert Path(cmd[idx + 1]) == progress
        at_spawn["exists"] = progress.exists()
        at_spawn["record"] = json.loads(progress.read_text(encoding="utf-8")) if progress.exists() else None

    rc, _ = _drive_default_round(monkeypatch, tmp_path, side_effect)

    assert rc != 0
    assert at_spawn["exists"] is True
    assert at_spawn["record"]["phase"] == "publish-running"
    assert at_spawn["record"]["driver_pid"] > 0


def test_driver_closes_its_own_record_with_the_outcome(monkeypatch, tmp_path):
    _drive_default_round(monkeypatch, tmp_path, lambda cmd, seen: None)

    record = json.loads((tmp_path / "publish-progress.json").read_text(encoding="utf-8"))
    assert record["phase"] == "publish-finished"
    assert record["returncode"] == 1


def test_a_raising_publish_leg_closes_the_record_as_failed(monkeypatch, tmp_path):
    import pytest

    def side_effect(cmd, seen):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(subprocess.TimeoutExpired):
        _drive_default_round(monkeypatch, tmp_path, side_effect)

    record = json.loads((tmp_path / "publish-progress.json").read_text(encoding="utf-8"))
    assert record["phase"] == "publish-failed" and record["error"] == "TimeoutExpired"


def test_driver_does_not_overwrite_a_record_the_child_wrote(monkeypatch, tmp_path):
    progress = tmp_path / "publish-progress.json"

    def side_effect(cmd, seen):
        progress.write_text(json.dumps({"writer": "publish", "staged": 3, "declared": 9}), encoding="utf-8")

    _drive_default_round(monkeypatch, tmp_path, side_effect)

    assert json.loads(progress.read_text(encoding="utf-8")) == {"writer": "publish", "staged": 3, "declared": 9}


def test_hang_dump_is_one_shot(monkeypatch):
    calls = []
    monkeypatch.setattr(pr, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(pr.faulthandler, "dump_traceback_later", lambda *a, **k: calls.append((a, k)))

    class _Parser:
        def parse_args(self, argv):
            return argparse.Namespace(open_only=False, no_publish=False, func=lambda a: 0)

    monkeypatch.setattr(pr, "_build_parser", lambda: _Parser())

    assert pr.main([]) == 0
    assert len(calls) == 1
    assert calls[0][1].get("repeat") is False
