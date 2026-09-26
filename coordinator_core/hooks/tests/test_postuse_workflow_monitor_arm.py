
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _installed_workflow_watch_launcher(tmp_path, monkeypatch):
    """Provision a settings-home `bin/workflow-watch` for the module under test.

    `_check_workflow_monitor_arm_sync` names the watcher by its ABSOLUTE
    installed launcher path and stays SILENT when that launcher is not on
    disk, so without this fixture every emission test would assert against ""
    on a box whose settings home has not been reinstalled since the launcher
    was added -- a green suite that proves only that the advisory is off.
    `test_stays_silent_when_no_launcher_is_installed` opts back out.
    """
    bin_dir = tmp_path / "settings-home" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "workflow-watch").write_text("stub launcher", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    yield str(bin_dir / "workflow-watch")


SESSION = "test-session-wf-monitor"


def _async_launched_record(task_id="task-abc", task_type="local_workflow", run_id="wf_123", transcript_dir="/tmp/wf-dir"):
    # Field order matches _ASYNC_LAUNCH_RE verbatim: status, taskId, taskType,
    payload = {
        "status": "async_launched",
        "taskId": task_id,
        "taskType": task_type,
        "runId": run_id,
        "transcriptDir": transcript_dir,
    }
    return "some prefix noise " + json.dumps(payload) + " some suffix noise\n"


def _write_transcript(tmp_path, *lines):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("".join(lines), encoding="utf-8")
    return str(transcript)


def test_fires_on_workflow_tool_with_well_formed_record(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record())

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result != ""
    assert "WORKFLOW MONITOR" in result


@pytest.mark.parametrize("tool_name", ["Agent", "Write", "Edit"])
def test_silent_on_other_tools_even_with_well_formed_record(tmp_path, tool_name):
    transcript_path = _write_transcript(tmp_path, _async_launched_record())

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, tool_name)

    assert result == ""


def test_silent_when_no_async_launched_record_found(tmp_path):
    transcript_path = _write_transcript(tmp_path, "just some ordinary transcript content\n")

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result == ""


def test_silent_when_task_type_is_not_local_workflow(tmp_path):
    transcript_path = _write_transcript(
        tmp_path, _async_launched_record(task_type="some_other_task_type")
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result == ""


def test_returns_empty_not_raises_on_unreadable_transcript():
    missing_path = os.path.join(tempfile.gettempdir(), "does-not-exist-wf-monitor.jsonl")
    assert not os.path.isfile(missing_path)

    result = pad._check_workflow_monitor_arm_sync(SESSION, missing_path, "Workflow")

    assert result == ""


def test_returns_empty_not_raises_on_malformed_transcript(tmp_path):
    transcript_path = _write_transcript(
        tmp_path, '{"status": "async_launched", "taskId": "unterminated\n'
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result == ""


def test_returns_empty_not_raises_when_tail_reader_itself_raises(tmp_path, monkeypatch):
    transcript_path = _write_transcript(tmp_path, _async_launched_record())

    class _BoomReader:
        def __init__(self, *_a, **_k):
            pass

        def poll(self):
            raise OSError("simulated read failure")

    monkeypatch.setattr(
        "coordinator_core.workflow_watch.tail.TailReader", _BoomReader
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result == ""


def test_advisory_names_finite_timeout_ms_and_never_persistent_true(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record())

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result != ""
    assert "timeout_ms=1800000" in result
    assert "persistent=true" not in result
    assert "persistent=false" in result


def test_fires_once_per_task_id_via_per_task_sentinel(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record(task_id="task-once"))

    first = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")
    assert first != ""

    second = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")
    assert second == ""

    tmpdir = tempfile.gettempdir()
    sentinel = pad._workflow_monitor_sentinel_path(tmpdir, SESSION, "task-once")
    assert os.path.isfile(sentinel)


def test_different_task_id_same_session_fires_again(tmp_path):
    first_transcript = _write_transcript(tmp_path, _async_launched_record(task_id="task-a"))
    first = pad._check_workflow_monitor_arm_sync(SESSION, first_transcript, "Workflow")
    assert first != ""

    second_transcript_dir = tmp_path / "second"
    second_transcript_dir.mkdir()
    second_transcript = str(second_transcript_dir / "transcript.jsonl")
    Path(second_transcript).write_text(_async_launched_record(task_id="task-b"), encoding="utf-8")

    second = pad._check_workflow_monitor_arm_sync(SESSION, second_transcript, "Workflow")
    assert second != ""


def test_never_touches_the_shared_advisory_hook_state_file(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record(task_id="task-disjoint"))
    shared_state_path = pad._advisory_state_path(tempfile.gettempdir(), SESSION)
    assert not os.path.isfile(shared_state_path)

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result != ""
    assert not os.path.isfile(shared_state_path)


def test_handler_five_way_merge_all_legs_fire_in_fixed_order(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record(task_id="task-merge"))

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            with mock.patch.object(
                pad, "_check_first_agent_dispatch_sync", return_value="agent text"
            ):
                with mock.patch.object(
                    pad.nudge_unauthorized_handoff,
                    "advisory_text",
                    mock.AsyncMock(return_value="[nudge] text"),
                ):
                    result = asyncio.run(
                        pad._handler(
                            {
                                "session_id": SESSION,
                                "transcript_path": transcript_path,
                                "tool_name": "Workflow",
                            }
                        )
                    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "agent text" in context
    assert "[nudge] text" in context
    assert "WORKFLOW MONITOR" in context

    assert (
        context.index("cp text")
        < context.index("rt text")
        < context.index("agent text")
        < context.index("[nudge] text")
        < context.index("WORKFLOW MONITOR")
    )


def test_handler_other_four_legs_fire_unaffected_by_non_workflow_tool(tmp_path):
    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            with mock.patch.object(
                pad, "_check_first_agent_dispatch_sync", return_value=""
            ):
                result = asyncio.run(
                    pad._handler({"session_id": SESSION, "tool_name": "Bash"})
                )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "WORKFLOW MONITOR" not in context


def test_handler_workflow_monitor_alone_still_post_advisory(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record(task_id="task-alone"))

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            with mock.patch.object(
                pad, "_check_first_agent_dispatch_sync", return_value=""
            ):
                result = asyncio.run(
                    pad._handler(
                        {
                            "session_id": SESSION,
                            "transcript_path": transcript_path,
                            "tool_name": "Workflow",
                        }
                    )
                )

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "WORKFLOW MONITOR" in hso["additionalContext"]


class _ExplodingOnUnexpectedKey(dict):

    _ALLOWED = {"session_id", "transcript_path", "agent_id", "tool_name", "file_path", "content"}

    def get(self, key, default=None):
        if key not in self._ALLOWED:
            raise AssertionError(f"unexpected params field read: {key!r}")
        return super().get(key, default)

    def __getitem__(self, key):
        if key not in self._ALLOWED:
            raise AssertionError(f"unexpected params field read: {key!r}")
        return super().__getitem__(key)


def test_handler_reads_no_params_field_beyond_the_six_mapped_fields(tmp_path):
    transcript_path = _write_transcript(
        tmp_path, _async_launched_record(task_id="task-negative-payload")
    )

    params = _ExplodingOnUnexpectedKey(
        {
            "session_id": SESSION,
            "transcript_path": transcript_path,
            "agent_id": "",
            "tool_name": "Workflow",
            "file_path": "",
            "content": "",
        }
    )

    result = asyncio.run(pad._handler(params))

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "WORKFLOW MONITOR" in context


def _launch_transcript(tmp_path, dirname="sub agents", session_name="my session.jsonl"):
    transcript_dir = tmp_path / dirname / "wf_abc123"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "journal.jsonl").write_text("", encoding="utf-8")
    transcript = tmp_path / session_name
    record = {
        "type": "assistant",
        "toolUseResult": {
            "status": "async_launched",
            "taskId": "tk777",
            "taskType": "local_workflow",
            "workflowName": "w",
            "runId": "wf_abc123",
            "transcriptDir": str(transcript_dir),
        },
    }
    transcript.write_text(
        json.dumps({"type": "user"}) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )
    return transcript


def _emitted_args(advisory):
    command = re.search(r'command="(.*?)", timeout_ms', advisory).group(1)
    argv = shlex.split(command)
    return dict(zip(argv[1::2], argv[2::2]))


def test_emitted_paths_survive_a_posix_shell_and_resolve(tmp_path):
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-quote", str(_launch_transcript(tmp_path)), "Workflow"
    )
    args = _emitted_args(advisory)
    assert os.path.isfile(args["--transcript"])
    assert os.path.isfile(args["--journal"])


def test_journal_path_is_json_decoded_not_raw_capture(tmp_path):
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-decode", str(_launch_transcript(tmp_path)), "Workflow"
    )
    journal = _emitted_args(advisory)["--journal"]
    assert "\\\\" not in journal
    assert journal.count("wf_abc123") == 1


def test_journal_path_does_not_double_append_the_run_id(tmp_path):
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-runid", str(_launch_transcript(tmp_path)), "Workflow"
    )
    assert os.path.isfile(_emitted_args(advisory)["--journal"])


def test_two_async_launch_records_in_one_tail_prefers_the_workflow(tmp_path):
    run_dir = tmp_path / "wf_abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text("", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    workflow_rec = {
        "type": "assistant",
        "toolUseResult": {
            "status": "async_launched", "taskId": "tk777",
            "taskType": "local_workflow", "runId": "wf_abc123",
            "transcriptDir": str(run_dir),
        },
    }
    other_rec = {
        "type": "assistant",
        "toolUseResult": {
            "status": "async_launched", "taskId": "tk999",
            "taskType": "local_agent", "runId": "wf_zzz999",
            "transcriptDir": str(run_dir),
        },
    }
    transcript.write_text(
        json.dumps(workflow_rec) + "\n" + json.dumps(other_rec) + "\n", encoding="utf-8"
    )
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-shadow", str(transcript), "Workflow"
    )
    assert advisory == ""


def test_sentinel_is_not_written_when_composition_never_completes(tmp_path, monkeypatch):
    transcript = _launch_transcript(tmp_path)

    def _boom():
        raise RuntimeError("composition failed")

    monkeypatch.setattr(pad, "_portable_arg", lambda _v: _boom())
    with pytest.raises(RuntimeError):
        pad._check_workflow_monitor_arm_sync("sess-nosent", str(transcript), "Workflow")

    leftovers = list(Path(tempfile.gettempdir()).glob("workflow-monitor-armed-sess-nosent-*"))
    assert leftovers == [], f"sentinel survived a failed composition: {leftovers}"


def test_emitted_args_carry_no_posix_only_quoting(tmp_path):
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-portable", str(_launch_transcript(tmp_path)), "Workflow"
    )
    command = re.search(r'command="(.*?)", timeout_ms', advisory).group(1)
    assert "'" not in command, command
    assert '\\' not in command, command


def test_unformattable_path_emits_nothing_rather_than_a_wrong_command(tmp_path):
    transcript = _launch_transcript(tmp_path, dirname='we$rd dir')
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-unsafe", str(transcript), "Workflow"
    )
    assert advisory == ""


def test_portable_arg_rejects_each_unsafe_character():
    for bad in ('a"b', 'a$b', 'a`b', 'a\nb'):
        assert pad._portable_arg(bad) is None, bad
    assert pad._portable_arg('C:\\Users\\a b\\x.jsonl') == '"C:/Users/a b/x.jsonl"'


def test_command_names_the_installed_launcher_not_a_bare_dash_m(tmp_path, _installed_workflow_watch_launcher):
    """The emitted command must be runnable from a CONSUMER repo.

    Regression pin for
    cross-repo/inbox/2026-08-30-doe-claude-em-workflow-watch-command-is-unrunnable-outside-the-engine.md
    (example-retrieval-repo-em via doe-claude-em): the command was composed as a literal
    `python3 -m coordinator_core.workflow_watch`, which exits 1 with
    `ModuleNotFoundError: No module named 'coordinator_core'` anywhere the
    engine is not already importable. The hook emitting it runs IN the engine's
    environment, so no test and no emitter-side check could see the failure --
    only the EM who pasted it, after the advisory's imperative wording had
    already talked them out of their own monitor.
    """
    transcript = _write_transcript(tmp_path, _async_launched_record(transcript_dir=str(tmp_path)))
    advisory = pad._check_workflow_monitor_arm_sync(SESSION, transcript, "Workflow")
    command = re.search(r'command="(.*?)", timeout_ms', advisory).group(1)
    argv = shlex.split(command)

    assert "python3" not in command
    assert "-m" not in argv
    assert argv[0] == _installed_workflow_watch_launcher.replace("\\", "/")
    assert os.path.isabs(argv[0])


def test_stays_silent_when_no_launcher_is_installed(tmp_path, monkeypatch):
    empty_home = tmp_path / "empty-settings-home"
    (empty_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_home))

    transcript = _write_transcript(tmp_path, _async_launched_record(transcript_dir=str(tmp_path)))
    assert pad._check_workflow_monitor_arm_sync(SESSION, transcript, "Workflow") == ""


def test_sentinel_is_not_written_when_the_launcher_is_missing(tmp_path, monkeypatch):
    """Silence must be RETRYABLE — a later reinstall has to be able to fire.

    The sentinel is written last precisely so an early-exit leaves nothing
    behind; if the missing-launcher branch wrote one, the advisory would stay
    suppressed for that task id forever, including after the launcher landed.
    """
    empty_home = tmp_path / "empty-settings-home-2"
    (empty_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_home))

    transcript = _write_transcript(tmp_path, _async_launched_record(transcript_dir=str(tmp_path)))
    pad._check_workflow_monitor_arm_sync(SESSION, transcript, "Workflow")

    sentinel = pad._workflow_monitor_sentinel_path(
        tempfile.gettempdir(), SESSION, "task-abc"
    )
    assert not os.path.isfile(sentinel)


def test_concurrent_launches_are_flagged_rather_than_guessed_through(tmp_path, capsys):
    transcript_path = _write_transcript(
        tmp_path,
        _async_launched_record(task_id="task-fire-1", run_id="wf_one"),
        _async_launched_record(task_id="task-fire-2", run_id="wf_two"),
        _async_launched_record(task_id="task-fire-3", run_id="wf_three"),
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert "WORKFLOW MONITOR" in result
    assert "task-fire-3" in result
    assert "CHECK BEFORE PASTING" in result
    assert "wf_three" in result
    assert "watches the wrong run" in result

    # breadcrumbed a DIFFERENT taskType, so the plan-blitz case — every
    err = capsys.readouterr().err
    assert "3 local_workflow launches" in err


def test_an_ambiguous_read_does_not_write_the_once_per_task_sentinel(tmp_path):
    transcript_path = _write_transcript(
        tmp_path,
        _async_launched_record(task_id="task-fire-1", run_id="wf_one"),
        _async_launched_record(task_id="task-fire-2", run_id="wf_two"),
    )

    first = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")
    second = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert first != ""
    assert second != "", "an ambiguous read must stay re-advisable, not self-suppress"

    sentinel = pad._workflow_monitor_sentinel_path(
        tempfile.gettempdir(), SESSION, "task-fire-2"
    )
    assert not os.path.isfile(sentinel)


def test_a_single_launch_is_unchanged_and_still_writes_its_sentinel(tmp_path, capsys):
    transcript_path = _write_transcript(
        tmp_path, _async_launched_record(task_id="task-solo", run_id="wf_solo")
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert "WORKFLOW MONITOR" in result
    assert "CHECK BEFORE PASTING" not in result
    assert "local_workflow launches" not in capsys.readouterr().err

    sentinel = pad._workflow_monitor_sentinel_path(
        tempfile.gettempdir(), SESSION, "task-solo"
    )
    assert os.path.isfile(sentinel)
    assert pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow") == ""


def test_a_repeated_launch_record_for_one_task_is_not_ambiguity(tmp_path, capsys):
    """Ambiguity is DISTINCT task ids, not record count. A transcript window
    holding the same launch twice (a re-read, or a resumed tail overlapping
    what it already saw) names one run and must take the ordinary path."""
    transcript_path = _write_transcript(
        tmp_path,
        _async_launched_record(task_id="task-same", run_id="wf_same"),
        _async_launched_record(task_id="task-same", run_id="wf_same"),
    )

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert "CHECK BEFORE PASTING" not in result
    assert "local_workflow launches" not in capsys.readouterr().err


def _record_with_script_path(task_id="task-cap", run_id="wf_cap1", script_path="a/b.workflow.mjs", args=None):
    payload = {
        "status": "async_launched",
        "taskId": task_id,
        "taskType": "local_workflow",
        "runId": run_id,
        "transcriptDir": "/tmp/wf-dir",
    }
    lines = ["some assistant tool_use noise "]
    if script_path is not None:
        lines.append(json.dumps({"scriptPath": script_path}))
    if args is not None:
        lines.append(json.dumps({"args": args}))
    lines.append(" some noise " + json.dumps(payload) + " trailing\n")
    return "".join(lines)


def test_persists_run_record_with_script_path_and_args(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    transcript_path = _write_transcript(
        tmp_path,
        _record_with_script_path(task_id="task-cap1", run_id="wf_cap1", script_path="a/b.workflow.mjs", args=["--x"]),
    )

    pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    record_path = pad._workflow_run_record_path(str(tmp_path), SESSION, "task-cap1")
    assert os.path.isfile(record_path)
    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["run_id"] == "wf_cap1"
    assert record["scriptPath"] == "a/b.workflow.mjs"
    assert record["args"] == ["--x"]
    assert record["session_id"] == SESSION
    assert isinstance(record["fired_at"], int)


def test_persists_run_record_with_scriptpath_missing_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    transcript_path = _write_transcript(
        tmp_path, _async_launched_record(task_id="task-cap2", run_id="wf_cap2")
    )

    pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    record_path = pad._workflow_run_record_path(str(tmp_path), SESSION, "task-cap2")
    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["run_id"] == "wf_cap2"
    assert record["scriptPath"] is None
    assert record["args"] is None


def test_persist_happens_even_when_monitor_arm_advisory_is_already_sentinelled(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    transcript_path = _write_transcript(
        tmp_path, _record_with_script_path(task_id="task-cap3", run_id="wf_cap3", script_path="a/b.workflow.mjs")
    )

    first = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")
    assert first != ""
    second = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")
    assert second == ""

    record_path = pad._workflow_run_record_path(str(tmp_path), SESSION, "task-cap3")
    assert os.path.isfile(record_path)


def test_persist_never_raises_on_unwritable_target(tmp_path):
    missing_dir = str(tmp_path / "does" / "not" / "exist")
    pad._persist_workflow_run_record(missing_dir, SESSION, "task-x", "wf_x", None, None)


def test_capture_script_path_and_args_defensive_on_garbage_text():
    assert pad._capture_script_path_and_args("not json at all {{{") == (None, None)


def test_capture_accepts_object_args():
    from coordinator_core.hooks.postuse_advisory_dispatch import _capture_script_path_and_args

    text = '{"scriptPath":"w/a.workflow.mjs","args":{"run_stamp":"x","ids":[1,2]}}'
    assert _capture_script_path_and_args(text) == (
        "w/a.workflow.mjs",
        {"run_stamp": "x", "ids": [1, 2]},
    )


def test_persisted_record_is_not_contaminated_by_a_later_unrelated_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    first = _record_with_script_path(
        task_id="task-first", run_id="wf_first", script_path="first.workflow.mjs", args=["--first"]
    )
    second = _record_with_script_path(
        task_id="task-second", run_id="wf_second", script_path="second.workflow.mjs", args=["--second"]
    )
    transcript_path = _write_transcript(tmp_path, first, second)

    pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    record_path = pad._workflow_run_record_path(str(tmp_path), SESSION, "task-second")
    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["scriptPath"] == "second.workflow.mjs"
    assert record["args"] == ["--second"]


def test_workflow_run_record_path_rejects_unsafe_task_id():
    with pytest.raises(ValueError):
        pad._workflow_run_record_path("/tmp", SESSION, "../../etc/passwd")
    with pytest.raises(ValueError):
        pad._workflow_run_record_path("/tmp", "not/safe", "task-ok")


def test_async_launch_regex_task_id_excludes_path_separators():
    """_ASYNC_LAUNCH_RE's taskId group must not admit '/', '\\\\', or '..' --
    the capture is now bounded to the same safe charset the path-builder
    enforces, so a hostile transcript value simply fails to match rather than
    reaching a path join."""
    text = _async_launched_record(task_id="../../etc/passwd")
    match = pad._ASYNC_LAUNCH_RE.search(text)
    if match is not None:
        assert "/" not in match.group("task_id")
        assert ".." not in match.group("task_id")
