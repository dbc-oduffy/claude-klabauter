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

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def test_fire_workflow_record_carries_publish_lag_message_field(repo, script, monkeypatch):
    """DR-335 call site (a): the field is always present on the record,
    `None` when the lag helper cannot establish a signal (no stamp, no
    resolvable sha, below threshold) -- never absent, never raising."""
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))

    assert "publish_lag_message" in record


def test_fire_workflow_surfaces_publish_lag_message_when_above_threshold(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fire, "_publish_lag_message", lambda cwd=None: "Engine lag: 4 commit(s) ...")

    record = fire.fire_workflow(str(script), cwd=str(repo))

    assert record["publish_lag_message"] == "Engine lag: 4 commit(s) ..."


def test_publish_lag_message_never_raises_when_skew_publish_lag_errors(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fire.skew, "publish_lag", boom)
    assert fire._publish_lag_message(str(repo)) is None


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
# Background-task wait ceiling -- the driver must not kill its own workflow
# ---------------------------------------------------------------------------


def test_build_fire_env_uncaps_background_wait():
    env = fire.build_fire_env({"PATH": "/usr/bin"})
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert env["PATH"] == "/usr/bin"


def test_build_fire_env_respects_an_explicit_operator_ceiling():
    env = fire.build_fire_env({"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "900000"})
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "900000"


def test_fire_workflow_spawns_child_with_uncapped_background_wait(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    captured = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return _FakePopen(command, **{})

    monkeypatch.setattr(fire.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    fire.fire_workflow(str(script), cwd=str(repo))

    assert captured["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"


def test_fire_workflow_child_env_is_not_narrowed(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setenv("COORDINATOR_FIRE_ENV_PROBE", "inherited")
    captured = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return _FakePopen(command, **{})

    monkeypatch.setattr(fire.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    fire.fire_workflow(str(script), cwd=str(repo))

    assert captured["env"]["COORDINATOR_FIRE_ENV_PROBE"] == "inherited"


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


# ---------------------------------------------------------------------------
# Record self-description -- staleness, outcome, exit-code honesty
#
# Spec backlink: state/handoffs/2026-08-19-fire-run-observability.md.
# The defect pinned here is reader-side: a raw read of a fire record reported
# a dead run as running, three times in one afternoon, because the record
# carried nothing marking `state` as a timestamped observation.
# ---------------------------------------------------------------------------

_CLEAN_ENVELOPE = '{"is_error":false,"stop_reason":"end_turn","type":"result","duration_ms":10}\n'
_TRUNCATION_LOG = (
    "Background tasks still running after 600s; terminating. Set "
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely.\n" + _CLEAN_ENVELOPE
)


def _exited_record(repo, log_text="", **overrides):
    """A registry record in the shape `_refresh_record_state` produces for a
    child whose pid is gone, with a log on disk to classify."""
    log_path = Path(repo) / "fire.log"
    log_path.write_text(log_text, encoding="utf-8")
    record = {
        "fire_id": "f" * 32,
        "pid": 999999,
        "state": "exited",
        "exit_code": None,
        "log_path": str(log_path),
        "log_size_bytes": len(log_text),
        "started_at": 1_000_000.0,
        "status_checked_at": 1_000_060.0,
    }
    record.update(overrides)
    return record


def test_raw_record_on_disk_states_that_state_is_only_valid_as_of_a_timestamp(
    repo, script, monkeypatch
):
    """The stale-read case. Fails against pre-2026-08-19 code, which wrote a
    bare `state` with nothing marking it as an observation."""
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))
    registry_path = fire._record_path(fire._registry_dir(str(repo)), record["fire_id"])
    raw = json.loads(registry_path.read_text(encoding="utf-8"))

    assert "status_checked_at" in raw["_readme"]
    assert "fire_status" in raw["_readme"]
    assert raw["status_checked_at_iso"].endswith("+00:00")


def test_refresh_annotates_a_record_written_before_the_annotation_existed(repo):
    """Self-healing migration -- a legacy record picks up its annotations on
    the next refresh, with no migration pass."""
    legacy = _exited_record(repo, log_text=_CLEAN_ENVELOPE)

    refreshed = fire._refresh_record_state(legacy)

    assert refreshed is not legacy
    assert "_readme" in refreshed


def test_exited_record_says_why_exit_code_is_null(repo):
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text=_CLEAN_ENVELOPE))

    assert refreshed["exit_code"] is None
    assert "detached" in refreshed["exit_code_note"]


def test_clean_finish_is_distinguishable_from_a_truncated_run(repo):
    """The exit code cannot separate these -- both are `None` -- so the
    classification comes off the child's own log."""
    clean = fire._refresh_record_state(_exited_record(repo, log_text=_CLEAN_ENVELOPE))
    assert clean["outcome"] == "clean"

    truncated = fire._refresh_record_state(_exited_record(repo, log_text=_TRUNCATION_LOG))
    assert truncated["outcome"] == "truncated"


def test_a_truncated_run_reporting_is_error_false_is_still_classified_truncated(repo):
    """Measured 2026-08-19 on fires 5df351a6 and 1eb8711b: a truncated run's
    envelope reads exactly like a clean one."""
    truncated = fire._refresh_record_state(_exited_record(repo, log_text=_TRUNCATION_LOG))

    assert '"is_error":false' in _TRUNCATION_LOG
    assert truncated["outcome"] == "truncated"
    assert "wait ceiling" in truncated["outcome_basis"]


def test_a_dead_child_that_never_wrote_an_envelope_is_truncated(repo):
    killed = fire._refresh_record_state(
        _exited_record(repo, log_text="partial output, no envelope\n")
    )

    assert killed["outcome"] == "truncated"


def test_a_running_record_carries_no_terminal_outcome(repo, script, monkeypatch):
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))

    assert record["state"] == "running"
    assert record["outcome"] == "unknown"


def test_a_settled_outcome_is_not_recomputed_from_the_log_on_later_sweeps(repo, monkeypatch):
    """The log read happens once, at the transition -- registry sweeps run on
    every fire under a 50-70 concurrent-LLM load norm."""
    settled = fire._refresh_record_state(_exited_record(repo, log_text=_CLEAN_ENVELOPE))

    def _fail(*_args, **_kwargs):
        raise AssertionError("outcome recomputed: the log was re-read on a later sweep")

    monkeypatch.setattr(fire, "_read_log_window", _fail)
    again = fire._refresh_record_state(settled)

    assert again["outcome"] == "clean"


def test_refresh_of_an_already_annotated_unchanged_record_does_not_churn_a_write(repo):
    """Identity is the callers' no-write signal (`count_live_fires`,
    `fire_status`) -- annotation must not break it."""
    settled = fire._refresh_record_state(_exited_record(repo, log_text=_CLEAN_ENVELOPE))

    assert fire._refresh_record_state(settled) is settled


def test_log_window_read_is_bounded_on_a_large_log(repo):
    """Head and tail both reach the classifier without a whole-file read."""
    log_path = Path(repo) / "big.log"
    filler = "x" * (fire._LOG_SCAN_WINDOW_BYTES * 3)
    log_path.write_text("HEAD-MARKER\n" + filler + "\nTAIL-MARKER\n", encoding="utf-8")

    window = fire._read_log_window(str(log_path))

    assert "HEAD-MARKER" in window
    assert "TAIL-MARKER" in window
    assert len(window) < len(filler)


def test_driver_session_id_is_lifted_from_the_terminal_envelope(repo):
    """The one hop to the per-phase truth. The envelope already carries it;
    a reader should not have to know that to find the workflow journal."""
    envelope = (
        '{"is_error":false,"type":"result","session_id":"6a9d7a24-1a9f-48ce-8879-c15d5258849c"}\n'
    )
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text=envelope))

    assert refreshed["driver_session_id"] == "6a9d7a24-1a9f-48ce-8879-c15d5258849c"


def test_a_child_with_no_envelope_carries_no_driver_session_id(repo):
    """Absent, never guessed -- a truncated child that died before writing
    its envelope has no session id to report."""
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text="no envelope here\n"))

    assert refreshed["outcome"] == "truncated"
    assert "driver_session_id" not in refreshed


def test_a_truncated_run_still_reports_its_driver_session_id(repo):
    """Truncation is exactly when a reader most needs the journal."""
    log = _TRUNCATION_LOG.replace(
        '"type":"result"', '"type":"result","session_id":"1ffc4db6-faf7-415b-a559-bc983abcd642"'
    )
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text=log))

    assert refreshed["outcome"] == "truncated"
    assert refreshed["driver_session_id"] == "1ffc4db6-faf7-415b-a559-bc983abcd642"


def test_terminal_envelope_returns_the_parsed_envelope_not_a_bare_flag(repo):
    assert fire._terminal_envelope(_CLEAN_ENVELOPE)["stop_reason"] == "end_turn"
    assert fire._terminal_envelope("nothing json here") is None
    assert fire._terminal_envelope('{"type":"assistant"}') is None


def test_a_record_settled_by_an_older_annotation_version_is_reclassified_once(repo, monkeypatch):
    """A record settled before `driver_session_id` existed picks it up on its
    next refresh -- and then stops re-reading its log."""
    envelope = '{"is_error":false,"type":"result","session_id":"e090f8be-c4a9-4fe9-99f5-000000000000"}\n'
    stale = _exited_record(repo, log_text=envelope)
    stale["outcome"] = "clean"
    stale["outcome_basis"] = "the driver wrote its own terminal result envelope"
    stale["_annotation_version"] = 1

    migrated = fire._refresh_record_state(stale)
    assert migrated["driver_session_id"] == "e090f8be-c4a9-4fe9-99f5-000000000000"
    assert migrated["_annotation_version"] == fire._ANNOTATION_VERSION

    def _fail(*_args, **_kwargs):
        raise AssertionError("log re-read after the version migration already ran")

    monkeypatch.setattr(fire, "_read_log_window", _fail)
    assert fire._refresh_record_state(migrated) is migrated


def test_an_exited_record_with_an_unreadable_log_stops_re_reading_it(repo, monkeypatch):
    """`unknown` on an already-exited record is terminal: the child is reaped
    and its log will never become readable. Re-attempting the open on every
    sweep is paid by every future fire, since `count_live_fires` walks the
    whole registry."""
    gone = _exited_record(repo, log_text="")
    gone["log_path"] = str(Path(repo) / "vanished.log")  # never created

    settled = fire._refresh_record_state(gone)
    assert settled["outcome"] == "unknown"
    assert "could not be read" in settled["outcome_basis"]

    def _fail(*_args, **_kwargs):
        raise AssertionError("unreadable log re-opened on a later sweep")

    monkeypatch.setattr(fire, "_read_log_window", _fail)
    assert fire._refresh_record_state(settled) is settled


def test_a_running_record_with_no_log_yet_keeps_re_checking(repo, monkeypatch):
    """The other half of the same rule: `unknown` on a record NOT yet observed
    to end must stay unsettled, or a live fire would never be classified."""
    live = _exited_record(repo, log_text="")
    live["state"] = "running"
    monkeypatch.setattr(fire, "_pid_alive", lambda _pid: True)

    refreshed = fire._refresh_record_state(live)

    assert refreshed["outcome"] == "unknown"
    assert "not been observed to end" in refreshed["outcome_basis"]


def test_the_banner_text_quoted_in_agent_output_is_not_a_truncation(repo):
    """This module's own source contains the banner verbatim, so a fired
    workflow that merely printed `fire.py` used to classify its own clean run
    as truncated."""
    quoted = (
        '    _TRUNCATION_BANNER = "Background tasks still running after"  # terminating\n'
        "  > Background tasks still running after 600s; terminating.\n"
        "the agent said Background tasks still running after 600s; terminating.\n"
    ) + _CLEAN_ENVELOPE
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text=quoted))

    assert refreshed["outcome"] == "clean"


def test_the_real_banner_on_its_own_line_still_classifies_truncated(repo):
    """The anchoring must not cost the true positives it was measured on."""
    refreshed = fire._refresh_record_state(_exited_record(repo, log_text=_TRUNCATION_LOG))

    assert refreshed["outcome"] == "truncated"


def test_multibyte_log_degrades_only_at_the_window_edges(repo):
    """Head and tail decode separately, so no SYNTHETIC seam is introduced
    mid-stream. A character split by the window boundary itself still costs
    one replacement char at that chunk's edge -- inherent to a bounded read,
    and bounded to the edges rather than loose in the middle."""
    log_path = Path(repo) / "multibyte.log"
    pad = "\u00e9" * fire._LOG_SCAN_WINDOW_BYTES
    log_path.write_text("HEAD-MARKER\n" + pad + "\nTAIL-MARKER\n", encoding="utf-8")

    window = fire._read_log_window(str(log_path))

    assert "HEAD-MARKER" in window
    assert "TAIL-MARKER" in window
    assert window.count("\ufffd") <= 2


def test_write_record_returns_exactly_what_landed_on_disk(repo, script, monkeypatch):
    """A caller holds the same dict the file holds, without annotating twice."""
    _patch_plugin_dir(monkeypatch)
    monkeypatch.setattr(fire.subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    monkeypatch.setattr(fire.time, "sleep", lambda *_: None)

    record = fire.fire_workflow(str(script), cwd=str(repo))
    registry_path = fire._record_path(fire._registry_dir(str(repo)), record["fire_id"])

    assert json.loads(registry_path.read_text(encoding="utf-8")) == record
