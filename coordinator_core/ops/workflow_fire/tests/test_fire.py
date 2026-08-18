"""
coordinator_core.ops.workflow_fire.tests.test_fire — acceptance surface
for the ``workflow.fire`` / ``workflow.fire_status`` op family.

Purpose: exercises ``fire.py``'s command-building, plugin-dir resolution,
registry read/write, liveness refresh, and concurrency-cap logic without
ever spawning a real ``claude`` child -- the spawn seam
(``subprocess.Popen``) and the plugin-dir resolution seam
(``subprocess.run``) are monkeypatched throughout, per this chunk's brief
("Your tests must NOT actually spawn a real `claude` child in the default
tier").

Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C4

Negative-spec:
  - Does NOT commit the spike's own probe scripts as tests -- these are
    fresh unit/integration tests against this package's real code, not a
    replay of the spike-verdict record's throwaway probes.
  - Does NOT spawn a real subprocess anywhere in this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.workflow_fire import fire


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePopen:
    """Stands in for ``subprocess.Popen`` -- never spawns a real process."""

    _instances = []

    def __init__(self, command, poll_sequence=None, pid=4242, **kwargs):
        self.args = command
        self.kwargs = kwargs
        self.pid = pid
        self._poll_sequence = list(poll_sequence) if poll_sequence is not None else [None]
        _FakePopen._instances.append(self)

    def poll(self):
        if len(self._poll_sequence) > 1:
            return self._poll_sequence.pop(0)
        return self._poll_sequence[0]


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    _FakePopen._instances = []
    yield
    _FakePopen._instances = []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repo -- ``sessions_dir`` resolves against it, giving
    each test its own isolated fire-registry directory."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # Real `git` behaviour is the assertion here: `fire.py`'s registry
    # placement depends on `sessions_dir()` -> `git --git-common-dir`
    # resolving correctly, which a hand-built `.git` directory would not
    # exercise faithfully.
    subprocess.run(
        ["git", "init", "-q", str(repo_dir)],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _cnw = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"],
        check=True,
        creationflags=_cnw,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "test"],
        check=True,
        creationflags=_cnw,
    )
    monkeypatch.chdir(repo_dir)
    return repo_dir


@pytest.fixture
def script(repo):
    p = repo / "workflow.mjs"
    p.write_text("// emitted workflow\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# build_fire_command -- `-p` is pinned, never omitted
# ---------------------------------------------------------------------------


def test_build_fire_command_never_omits_dash_p():
    command = fire.build_fire_command("script.mjs", "/plugins/coordinator")
    assert "-p" in command
    assert command[0:2] == ["claude", "-p"]


def test_build_fire_command_never_uses_bg():
    command = fire.build_fire_command("script.mjs", "/plugins/coordinator")
    assert "--bg" not in command


def test_build_fire_command_includes_resolved_plugin_dir_and_allowed_tools():
    command = fire.build_fire_command("script.mjs", "/resolved/plugin/dir", model="haiku", max_turns=3)
    assert "--allowedTools" in command
    assert "--plugin-dir" in command
    assert command[command.index("--plugin-dir") + 1] == "/resolved/plugin/dir"
    assert "--model" in command
    assert command[command.index("--model") + 1] == "haiku"
    assert "--max-turns" in command
    assert command[command.index("--max-turns") + 1] == "3"


def test_build_fire_command_allowed_tools_grant_is_widened_beyond_workflow():
    """Two-probe live finding: bare `--allowedTools Workflow` scopes the
    whole fired session, so a spawned executor/committer phase's own
    Write/Edit/Bash was refused even though the driver's Workflow call
    succeeded. The session grant must be wide enough not to be the
    binding constraint for an ordinary phase shape."""
    command = fire.build_fire_command("script.mjs", "/resolved/plugin/dir")
    idx = command.index("--allowedTools")
    next_flag_idx = command.index("--plugin-dir")
    granted = set(command[idx + 1 : next_flag_idx])
    assert {"Workflow", "Read", "Write", "Edit", "Bash", "Grep", "Glob", "ToolSearch"} <= granted


def test_build_fire_command_never_uses_dangerously_skip_permissions():
    command = fire.build_fire_command("script.mjs", "/resolved/plugin/dir")
    assert "--dangerously-skip-permissions" not in command


# ---------------------------------------------------------------------------
# resolve_plugin_dir -- native resolver primary, shim fallback, fail loud
# when both fail. `--print-plugin-dir` is a SHIM flag, not a `claude` flag
# -- the raw `claude` binary `shutil.which` can resolve to on Windows does
# not understand it (live coordinator finding). The native, in-process
# `coordinator_doe_root()` resolver is the primary path so the happy path
# never depends on shelling out at all.
# ---------------------------------------------------------------------------


def test_resolve_plugin_dir_prefers_native_resolver(monkeypatch):
    monkeypatch.setattr(fire, "_native_plugin_dir", lambda: "/native/resolved/coordinator")

    def fail_if_called(shim_bin="claude-doe"):
        raise AssertionError("shim fallback must not run when the native resolver succeeds")

    monkeypatch.setattr(fire, "_shim_plugin_dir", fail_if_called)
    assert fire.resolve_plugin_dir() == "/native/resolved/coordinator"


def test_resolve_plugin_dir_unmocked_against_real_resolver():
    """Exercises `resolve_plugin_dir` against the real `coordinator_doe_root()`
    resolver, unmocked -- the gap that let the live Windows defect through
    was that every other test here stubs this seam. Skips cleanly (rather
    than failing) when no coordinator plugin install resolves at all --
    e.g. a fresh clone with no DoE-claude checkout -- since that is an
    install-surface gap, not a `resolve_plugin_dir` defect."""
    try:
        resolved = fire.resolve_plugin_dir()
    except fire.PluginDirResolutionError:
        pytest.skip("no coordinator plugin install resolves on this machine")
    assert Path(resolved).is_dir()
    assert Path(resolved).name == "coordinator"


def test_resolve_plugin_dir_falls_back_to_shim_when_native_fails(monkeypatch):
    monkeypatch.setattr(fire, "_native_plugin_dir", lambda: None)

    def fake_shim(shim_bin="claude-doe"):
        assert shim_bin == "claude-doe"
        return "/shim/resolved/coordinator"

    monkeypatch.setattr(fire, "_shim_plugin_dir", fake_shim)
    assert fire.resolve_plugin_dir() == "/shim/resolved/coordinator"


def test_shim_plugin_dir_never_invokes_bare_claude(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="/home/user/.claude/plugins/coordinator\n")

    monkeypatch.setattr(fire.subprocess, "run", fake_run)
    assert fire._shim_plugin_dir() == "/home/user/.claude/plugins/coordinator"
    assert captured["cmd"][0] == "claude-doe"
    assert captured["cmd"][0] != "claude"


def test_shim_plugin_dir_returns_none_on_nonzero_exit(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=1, stdout="", stderr="unknown option '--print-plugin-dir'")

    monkeypatch.setattr(fire.subprocess, "run", fake_run)
    assert fire._shim_plugin_dir() is None


def test_shim_plugin_dir_returns_none_on_blank_stdout(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="   \n")

    monkeypatch.setattr(fire.subprocess, "run", fake_run)
    assert fire._shim_plugin_dir() is None


def test_shim_plugin_dir_returns_none_on_missing_binary(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: claude-doe")

    monkeypatch.setattr(fire.subprocess, "run", fake_run)
    assert fire._shim_plugin_dir() is None


def test_resolve_plugin_dir_fails_loud_when_both_paths_fail(monkeypatch):
    monkeypatch.setattr(fire, "_native_plugin_dir", lambda: None)
    monkeypatch.setattr(fire, "_shim_plugin_dir", lambda shim_bin="claude-doe": None)
    with pytest.raises(fire.PluginDirResolutionError):
        fire.resolve_plugin_dir()


# ---------------------------------------------------------------------------
# fire_workflow -- happy path, registry record shape, liveness confirmation
# ---------------------------------------------------------------------------


def _patch_plugin_dir(monkeypatch, plugin_dir="/plugins/coordinator"):
    monkeypatch.setattr(fire, "resolve_plugin_dir", lambda claude_bin="claude": plugin_dir)


def test_fire_workflow_returns_registry_record_not_bare_pid(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))

    assert record["state"] == "running"
    assert record["pid"] == 4242
    assert record["script_path"] == str(script)
    assert record["plugin_dir"] == "/plugins/coordinator"
    assert "-p" in record["command"]
    assert "--bg" not in record["command"]
    assert record["fire_id"]
    assert Path(record["log_path"]).parent.is_dir()


def test_fire_workflow_persists_record_readable_by_fire_status(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))
    registry_path = fire._record_path(fire._registry_dir(str(repo)), record["fire_id"])
    on_disk = json.loads(registry_path.read_text(encoding="utf-8"))
    assert on_disk["fire_id"] == record["fire_id"]
    assert on_disk["state"] == "running"


def test_fire_workflow_script_not_found_raises(repo, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    with pytest.raises(fire.ScriptNotFoundError):
        fire.fire_workflow(str(repo / "missing.mjs"), cwd=str(repo))


def test_fire_workflow_plugin_dir_resolution_failure_propagates(repo, script, monkeypatch):
    def fail(claude_bin="claude"):
        raise fire.PluginDirResolutionError("boom")

    monkeypatch.setattr(fire, "resolve_plugin_dir", fail)
    with pytest.raises(fire.PluginDirResolutionError):
        fire.fire_workflow(str(script), cwd=str(repo))


def test_fire_workflow_immediate_nonzero_exit_raises_not_returns_handle(repo, script, monkeypatch):
    """A child that dies at spawn must not look identical to one still
    running -- it must raise, never return a record claiming "running"."""
    _patch_plugin_dir(monkeypatch)

    def make_popen(*a, **k):
        return _FakePopen(*a, poll_sequence=[7], **k)

    monkeypatch.setattr(fire.subprocess, "Popen", make_popen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    with pytest.raises(fire.ChildSpawnFailedError):
        fire.fire_workflow(str(script), cwd=str(repo))


def test_fire_workflow_spawn_oserror_raises_child_spawn_failed(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)

    def raising_popen(*a, **k):
        raise OSError("no such binary")

    monkeypatch.setattr(fire.subprocess, "Popen", raising_popen)
    with pytest.raises(fire.ChildSpawnFailedError):
        fire.fire_workflow(str(script), cwd=str(repo))


# ---------------------------------------------------------------------------
# Concurrency cap -- refuse once live-fire count is at or above K
# ---------------------------------------------------------------------------


def test_fire_workflow_refuses_once_cap_reached(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fire, "_pid_alive", lambda pid: True)

    for _ in range(2):
        fire.fire_workflow(str(script), cwd=str(repo), concurrency_cap=2)

    with pytest.raises(fire.ConcurrencyCapExceededError):
        fire.fire_workflow(str(script), cwd=str(repo), concurrency_cap=2)


def test_fire_workflow_does_not_count_exited_fires_against_cap(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fire, "_pid_alive", lambda pid: False)

    for _ in range(3):
        fire.fire_workflow(str(script), cwd=str(repo), concurrency_cap=2)


def test_default_concurrency_cap_constant_is_three():
    assert fire.DEFAULT_CONCURRENCY_CAP == 3


# ---------------------------------------------------------------------------
# fire_status -- reads and refreshes registry state
# ---------------------------------------------------------------------------


def test_fire_status_returns_none_for_unknown_fire_id(repo):
    assert fire.fire_status("does-not-exist", cwd=str(repo)) is None


def test_fire_status_refreshes_dead_pid_to_exited(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))
    assert record["state"] == "running"

    monkeypatch.setattr(fire, "_pid_alive", lambda pid: False)
    refreshed = fire.fire_status(record["fire_id"], cwd=str(repo))
    assert refreshed["state"] == "exited"
    assert refreshed["exit_code"] is None


def test_fire_status_leaves_live_pid_running(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))
    monkeypatch.setattr(fire, "_pid_alive", lambda pid: True)
    refreshed = fire.fire_status(record["fire_id"], cwd=str(repo))
    assert refreshed["state"] == "running"


# ---------------------------------------------------------------------------
# Windows detachment flags -- explicit, not incidental
# ---------------------------------------------------------------------------


def test_win32_spawn_uses_detached_process_group_flags(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire, "sys", SimpleNamespace(platform="win32"))
    captured = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return _FakePopen(command, **{})

    monkeypatch.setattr(fire.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    fire.fire_workflow(str(script), cwd=str(repo))

    assert "creationflags" in captured
    assert captured["creationflags"] & fire._DETACHED_PROCESS
    assert captured["creationflags"] & fire._CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in captured


def test_posix_spawn_uses_start_new_session(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire, "sys", SimpleNamespace(platform="linux"))
    captured = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return _FakePopen(command, **{})

    monkeypatch.setattr(fire.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    fire.fire_workflow(str(script), cwd=str(repo))

    assert captured.get("start_new_session") is True
    assert "creationflags" not in captured
