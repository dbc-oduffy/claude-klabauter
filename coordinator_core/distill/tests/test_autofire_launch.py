"""Tests for autofire_launch: arguments handed to fire_workflow, and the real argv it builds."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from coordinator_core.distill import autofire_launch
from coordinator_core.ops.workflow_fire import fire


def _capture(monkeypatch):
    calls = []

    def fake(script_path, **kw):
        calls.append((script_path, kw))
        return {"fire_id": "f1", "state": "running"}

    monkeypatch.setattr(fire, "fire_workflow", fake)
    return calls


def test_launch_passes_headless_arguments(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    record = autofire_launch.launch(tmp_path)
    assert record == {"fire_id": "f1", "state": "running"}
    (script, kw), = calls
    root = tmp_path.resolve()
    assert Path(script) == root / ".coordinator-local" / "distill-autofire" / "fire.mjs"
    assert Path(script).is_file()
    assert kw["cwd"] == str(root)
    assert kw["wait"] is False
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}h\d{2}", kw["run_id"])
    assert "/distill" in kw["prompt"] and kw["run_id"] in kw["prompt"]
    assert "No PM gate" in kw["prompt"]


def test_launch_never_passes_expected_tag(tmp_path, monkeypatch):
    """`expected` is a sha256 to fire_workflow; a tag string would raise before spawn."""
    calls = _capture(monkeypatch)
    autofire_launch.launch(tmp_path)
    assert "expected" not in calls[0][1]


def test_script_not_under_state(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    autofire_launch.launch(tmp_path)
    assert "/state/" not in Path(calls[0][0]).as_posix()


def test_launch_overwrites_one_fixed_script(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    autofire_launch.launch(tmp_path)
    autofire_launch.launch(tmp_path)
    assert calls[0][0] == calls[1][0]
    assert len(list((tmp_path / ".coordinator-local" / "distill-autofire").iterdir())) == 1


def test_launch_propagates_fire_failure(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise fire.ChildSpawnFailedError("nope")

    monkeypatch.setattr(fire, "fire_workflow", boom)
    with pytest.raises(fire.ChildSpawnFailedError):
        autofire_launch.launch(tmp_path)


def test_mint_run_id_format():
    now = datetime.datetime(2026, 11, 13, 4, 7)
    assert autofire_launch.mint_run_id(now) == "2026-11-13-04h07"


def test_prompt_only_script_accepted_by_real_command_builder(tmp_path):
    """Spike: render/build accept a prompt override with the marker script (no spawn)."""
    script = autofire_launch.fire_script_path(tmp_path)
    script.parent.mkdir(parents=True)
    script.write_text(autofire_launch._SCRIPT_BODY, encoding="utf-8")
    prompt = autofire_launch._PROMPT.format(run_id="2026-11-13-04h07")
    argv = fire.build_fire_command(str(script), "plugin-dir", prompt=prompt, add_dir=str(tmp_path))
    assert argv[:3] == ["claude", "-p", prompt]
    fire._assert_script_under_repo_root(script, str(tmp_path))
    assert fire._count_phases(autofire_launch._SCRIPT_BODY) == 0
