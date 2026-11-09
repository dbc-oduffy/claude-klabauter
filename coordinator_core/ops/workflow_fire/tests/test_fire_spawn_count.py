"""Exact-equality spawn count and process-time bar for `workflow.fire`'s detached-fire path.

The child `claude -p` is stubbed: it is background work outside the op's budget. Every other
spawn the op makes (git, the plugin-dir shim) goes through the real `subprocess.Popen` and is
recorded. The figures are read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.ops.workflow_fire import fire

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CHILD_BIN = "stub-claude-child"
_SHIM_BIN = "claude-author"


def _row() -> dict:
    return load_manifest()["overrides"]["workflow.fire"]


class _StubProcess:
    """Stands in for the detached child and for the `claude-author` shim run."""

    pid = 4242
    returncode = 0
    args: tuple = ()

    def __init__(self, stdout: str = ""):
        self._stdout = stdout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def poll(self):
        return None if self._stdout == "" else 0

    def wait(self, timeout=None):
        return 0

    def communicate(self, input=None, timeout=None):
        return self._stdout, ""

    def kill(self):
        pass


@pytest.fixture
def fire_target(tmp_path):
    cnw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, creationflags=cnw)
    script = repo / "workflow.mjs"
    script.write_text("// emitted workflow\n", encoding="utf-8")
    return repo, script


def _fire(monkeypatch, fire_target, *, native_dir):
    """Run the wait=False fire; return (record, [argv of every Popen the op started])."""
    repo, script = fire_target
    monkeypatch.setattr(fire, "_native_plugin_dir", lambda: native_dir)
    monkeypatch.setattr(fire.time, "sleep", lambda _s: None)
    shim_dir = str(repo)
    real_popen = subprocess.Popen
    started: list[tuple[str, ...]] = []

    def _popen(args, *a, **kw):
        argv = tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),)
        started.append(argv)
        if argv[0] == _CHILD_BIN:
            return _StubProcess()
        if argv[0] == _SHIM_BIN:
            return _StubProcess(stdout=shim_dir + "\n")
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _popen)
    record = fire.fire_workflow(str(script), cwd=str(repo), claude_bin=_CHILD_BIN)
    return record, started


def test_native_plugin_dir_path_spawns_only_the_child(monkeypatch, fire_target):
    record, started = _fire(monkeypatch, fire_target, native_dir=str(fire_target[0]))

    assert record["state"] == "running", record
    assert [a[0] for a in started] == [_CHILD_BIN], started
    assert len(started) == _row()["spawn_count_budget"]["plugin_dir_resolved"], started


def test_shim_fallback_path_adds_exactly_the_shim_spawn(monkeypatch, fire_target):
    record, started = _fire(monkeypatch, fire_target, native_dir=None)

    assert record["state"] == "running", record
    assert [a[0] for a in started] == [_SHIM_BIN, _CHILD_BIN], started
    assert len(started) == _row()["spawn_count_budget"]["plugin_dir_shim_fallback"], started


def test_op_process_time_holds_the_row_target(monkeypatch, fire_target):
    row = _row()
    start = time.process_time()
    _fire(monkeypatch, fire_target, native_dir=str(fire_target[0]))
    elapsed_ms = (time.process_time() - start) * 1000.0

    bar = row["target_ms"] * (1 + row["tolerance"]["value"])
    assert elapsed_ms <= bar, f"workflow.fire process time {elapsed_ms:.1f}ms over {bar:.0f}ms"
