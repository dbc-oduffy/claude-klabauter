"""The scratch-size line in the session-start payload: when it speaks, what it says, how it runs."""

from __future__ import annotations

import asyncio
import json
import shlex
import threading
from pathlib import Path

import pytest

from coordinator_core._settings_home import settings_home
from coordinator_core.hooks import project_orientation as po
from coordinator_core.hooks import sessionstart_dispatch as sd
from coordinator_core.hooks.scratch_size_probe import (
    SCRATCH_WARN_BYTES,
    WALK_ENTRY_LIMIT,
    ScratchRead,
)

_FORBIDDEN = ("too large to measure", "rm ", "rm -", "rmdir", "del ", "Remove-Item")
_MB5 = 5 * 1024 * 1024


@pytest.fixture(autouse=True)
def _no_real_launch_or_disk_read(monkeypatch):
    monkeypatch.setattr(po, "_free_fraction", lambda root: 0.5)
    monkeypatch.setattr(po, "_launch_scratch_hygiene", lambda root, params: False)


def _banner(root, read=None, monkeypatch=None):
    if read is not None:
        monkeypatch.setattr(po, "read_scratch", lambda roots, **kw: read)
    out: list = []
    po._scratch_size_banner(out, None if root is None else str(root))
    return out


def _lines(out):
    return [x for x in out if "Scratch" in x]


def test_under_threshold_real_tree_adds_nothing(tmp_path):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "a.txt").write_bytes(b"hi")
    assert _lines(_banner(tmp_path)) == []


def test_no_scratch_and_no_root_add_nothing(tmp_path):
    assert _banner(tmp_path) == []
    assert _banner(None) == []


def test_exactly_at_threshold_finished_adds_nothing(tmp_path, monkeypatch):
    assert _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES), monkeypatch) == []


def test_over_line(tmp_path, monkeypatch):
    out = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)
    assert len(out) == 1
    line = out[0]
    assert line.startswith("── ⚠ Scratch is")
    assert "fleet.scratch_hygiene" in line and "rerun" in line
    assert str(tmp_path / "scratch-hold") in line


def test_over_count_line(tmp_path, monkeypatch):
    over = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)[0]
    out = _banner(tmp_path, ScratchRead(False, WALK_ENTRY_LIMIT, _MB5, True), monkeypatch)
    assert len(out) == 1
    line = out[0]
    assert line.startswith("── ⚠ Scratch holds over")
    assert str(WALK_ENTRY_LIMIT) in line and "5.0 MB" in line
    assert "Scratch is" not in line
    assert line[line.index("fleet.scratch_hygiene"):] == over[over.index("fleet.scratch_hygiene"):]


def test_time_stop_line(tmp_path, monkeypatch):
    over = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)[0]
    out = _banner(tmp_path, ScratchRead(False, 1234, _MB5), monkeypatch)
    assert len(out) == 1
    line = out[0]
    assert line.startswith("── ⚠ Scratch read stopped at its time allowance")
    assert "1234" in line and "5.0 MB" in line
    assert "Scratch is" not in line
    assert line[line.index("fleet.scratch_hygiene"):] == over[over.index("fleet.scratch_hygiene"):]


@pytest.mark.parametrize(
    "read",
    [
        ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1),
        ScratchRead(False, WALK_ENTRY_LIMIT, _MB5, True),
        ScratchRead(False, 1234, _MB5),
    ],
)
def test_lines_name_no_delete_command_and_one_remedy(tmp_path, monkeypatch, read):
    line = _banner(tmp_path, read, monkeypatch)[0]
    for token in _FORBIDDEN:
        assert token not in line
    assert "cleared by hand" not in line
    assert "only" not in line.split("resolve:", 1)[1]
    assert f"{tmp_path / 'scratch-hold'}/" in line
    assert line.count("fleet.scratch_hygiene") == 1


def test_handler_carries_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr(
        po, "read_scratch", lambda roots, **kw: ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1)
    )
    monkeypatch.setattr(po, "_resolve_repo_root", lambda env, cwd: str(tmp_path))
    res = po._handler({"payload": {"cwd": str(tmp_path), "env": {}}})
    assert "── ⚠ Scratch is" in json.dumps(res, ensure_ascii=False)


def test_raising_read_leaves_payload_intact(tmp_path, monkeypatch):
    def boom(roots, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(po, "read_scratch", boom)
    monkeypatch.setattr(po, "_resolve_repo_root", lambda env, cwd: str(tmp_path))
    res = po._handler({"payload": {"cwd": str(tmp_path), "env": {}}})
    assert "Scratch" not in json.dumps(res)


def test_state_scratch_alone_trips_the_over_line(tmp_path, monkeypatch):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "state" / "scratch").mkdir(parents=True)
    (tmp_path / "state" / "scratch" / "a").write_bytes(b"x" * 100)
    monkeypatch.setattr(po, "SCRATCH_WARN_BYTES", 50)
    out = _banner(tmp_path)
    assert len(out) == 1 and out[0].startswith("── ⚠ Scratch is")


def test_banner_reads_once_with_both_roots_in_order(tmp_path, monkeypatch):
    seen = []

    def fake(roots, **kw):
        seen.append(list(roots))
        return ScratchRead(True, 0, 0)

    monkeypatch.setattr(po, "read_scratch", fake)
    po._scratch_size_banner([], str(tmp_path))
    assert seen == [[tmp_path / "scratch", tmp_path / "state" / "scratch"]]


def test_leg_returns_awaitable_and_handler_runs_it_off_the_loop(monkeypatch):
    seen = {}

    def fake(params):
        seen["tid"] = threading.get_ident()
        return "ORIENTATION-TEXT"

    monkeypatch.setattr(sd, "_project_orientation_handler", fake)
    import inspect

    aw = sd._leg_project_orientation({"source": "startup"})
    assert inspect.isawaitable(aw)
    asyncio.run(aw)

    async def run():
        loop_tid = threading.get_ident()
        res = await sd._handler({"payload": {"source": "startup"}})
        return loop_tid, res

    seen.clear()
    loop_tid, res = asyncio.run(run())
    assert "ORIENTATION-TEXT" in json.dumps(res)
    assert seen["tid"] != loop_tid


def test_register_op_decorates_the_handler_not_a_helper():
    import importlib

    from coordinator_core.ipc import _REGISTRY

    live = importlib.import_module("coordinator_core.hooks.project_orientation")
    registered = _REGISTRY["hooks.project_orientation"]
    assert registered is live._handler
    assert registered.__module__ == live.__name__
    assert registered.__name__ == "_handler"


def test_rendered_remedy_is_three_shell_tokens(tmp_path, monkeypatch):
    line = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)[0]
    remedy = line.split("resolve: ", 1)[1]
    cut = remedy[: remedy.index(" (rerun")]
    tokens = shlex.split(cut)
    assert len(tokens) == 3
    assert tokens[0] == str(settings_home() / "bin" / "coordinator-invoke")
    assert tokens[1] == "fleet.scratch_hygiene"
    assert json.loads(tokens[2]) == {
        "repo_root": str(tmp_path),
        "cadence": "workday_complete",
        "apply": True,
    }


def _recording_launcher(monkeypatch, ok=True):
    calls = []

    def fake(root, params):
        calls.append((root, json.loads(params)))
        return ok

    monkeypatch.setattr(po, "_launch_scratch_hygiene", fake)
    return calls


def test_over_size_launches_the_op_once_and_says_so(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    out = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)
    assert calls == [
        (str(tmp_path), {"repo_root": str(tmp_path), "cadence": "workday_complete", "apply": True})
    ]
    assert "cleanup started in the background" in out[0]
    assert out[0].startswith("── ⚠ Scratch is")


def test_entry_bound_launches(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    _banner(tmp_path, ScratchRead(False, WALK_ENTRY_LIMIT, _MB5, True), monkeypatch)
    assert len(calls) == 1


def test_under_both_thresholds_launches_nothing(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    assert _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES), monkeypatch) == []
    assert calls == []


def test_time_stop_under_threshold_warns_without_launching(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    out = _banner(tmp_path, ScratchRead(False, 1234, _MB5), monkeypatch)
    assert len(out) == 1 and calls == []


def test_low_disk_alone_launches_with_small_scratch(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    monkeypatch.setattr(po, "_free_fraction", lambda root: 0.04)
    out = _banner(tmp_path, ScratchRead(True, 3, _MB5), monkeypatch)
    assert len(calls) == 1
    assert out[0].startswith("── ⚠ Scratch is 5.0 MB") and "4% free" in out[0]


def test_disk_exactly_at_threshold_is_not_low(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    monkeypatch.setattr(po, "_free_fraction", lambda root: po.SCRATCH_LOW_DISK_FREE_FRACTION)
    assert _banner(tmp_path, ScratchRead(True, 3, _MB5), monkeypatch) == []
    assert calls == []


def test_unreadable_disk_does_not_trigger(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    monkeypatch.setattr(po, "_free_fraction", lambda root: None)
    assert _banner(tmp_path, ScratchRead(True, 3, _MB5), monkeypatch) == []
    assert calls == []


def test_free_fraction_reads_disk_usage_and_survives_failure(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert 0.0 <= po._free_fraction(str(tmp_path)) <= 1.0
    assert po._free_fraction(str(tmp_path / "missing")) is None


def test_second_trigger_inside_the_interval_only_warns(tmp_path, monkeypatch):
    calls = _recording_launcher(monkeypatch)
    big = ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1)
    _banner(tmp_path, big, monkeypatch)
    out = _banner(tmp_path, big, monkeypatch)
    assert len(calls) == 1
    assert "cleanup started" not in out[0] and "resolve:" in out[0]


def test_trigger_fires_again_after_the_interval(tmp_path, monkeypatch):
    import os

    calls = _recording_launcher(monkeypatch)
    big = ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1)
    _banner(tmp_path, big, monkeypatch)
    stamp = tmp_path / po._SCRATCH_TRIGGER_STAMP
    old = stamp.stat().st_mtime - po.SCRATCH_TRIGGER_INTERVAL_SECS - 1
    os.utime(stamp, (old, old))
    _banner(tmp_path, big, monkeypatch)
    assert len(calls) == 2


def test_failed_launch_keeps_the_plain_remedy(tmp_path, monkeypatch):
    _recording_launcher(monkeypatch, ok=False)
    out = _banner(tmp_path, ScratchRead(True, 3, SCRATCH_WARN_BYTES + 1), monkeypatch)
    assert "resolve:" in out[0] and "cleanup started" not in out[0]


def test_launcher_spawns_detached_with_the_op_and_params(tmp_path, monkeypatch):
    import subprocess

    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert po._launch_scratch_hygiene(str(tmp_path), '{"a": 1}') is True
    assert seen["argv"][1:] == ["fleet.scratch_hygiene", '{"a": 1}']
    assert seen["kw"]["stdout"] is subprocess.DEVNULL and seen["kw"]["cwd"] == str(tmp_path)
    assert seen["kw"].get("start_new_session") or "creationflags" in seen["kw"]

    def boom(*a, **k):
        raise OSError("no binary")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert po._launch_scratch_hygiene(str(tmp_path), "{}") is False
