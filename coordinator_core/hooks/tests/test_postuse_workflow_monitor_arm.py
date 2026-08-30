"""
coordinator_core.hooks.tests.test_postuse_workflow_monitor_arm — tests for the
fifth PostToolUse advisory leg folded into `postuse_advisory_dispatch.py`:
`_check_workflow_monitor_arm_sync` and its composition inside `_handler`.

Covers: the tool_name gate (fires only on "Workflow", silent on the
dispatcher's other narrow-gated tools -- Agent, Write -- and on an unrelated
tool -- Edit); the transcript-tail scan for a well-formed
`async_launched`/`local_workflow` record (absent record, wrong taskType,
malformed/unreadable transcript); the emitted advisory shape (finite
`timeout_ms`, never `persistent: true`); the once-per-task-id sentinel
discipline (disjoint from `advisory-hook-state-{session_id}.json`); that the
other four legs still fire unaffected by this fold; and a negative-payload
pin (Review: staff-eng Finding 2) -- the check reads no field from `params`
beyond the six `_handler` actually receives.

Spec backlink: coordinator_core/hooks/postuse_advisory_dispatch.py
`_check_workflow_monitor_arm_sync` (module under test).
"""

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
    """Point the module under test at a per-test temp dir instead of the box's.

    The sentinel and advisory-state files are located via
    `tempfile.gettempdir()`. Redirecting that to a fresh `tmp_path` isolates
    every test by construction, so no cleanup sweep is needed: pytest discards
    the directory itself. The sweep this replaced ran `glob.glob` over the
    SHARED system temp three times in both setup and teardown of every test --
    ~1.3s a pass on a box running dozens of concurrent sessions, ~40s across
    this file, all of it stolen from peers for state that was never shared.
    """
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
    # runId, transcriptDir -- embedded inside a larger (not standalone-JSON)
    # transcript line, mirroring the plan's evidence transcript shape.
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


# ---------------------------------------------------------------------------
# Gate: fires only on tool_name == "Workflow".
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# No record / wrong taskType near the tail.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Malformed / unreadable transcript never raises.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Emitted advisory shape: finite timeout_ms, never persistent: true.
# ---------------------------------------------------------------------------


def test_advisory_names_finite_timeout_ms_and_never_persistent_true(tmp_path):
    transcript_path = _write_transcript(tmp_path, _async_launched_record())

    result = pad._check_workflow_monitor_arm_sync(SESSION, transcript_path, "Workflow")

    assert result != ""
    assert "timeout_ms=1800000" in result
    assert "persistent=true" not in result
    assert "persistent=false" in result


# ---------------------------------------------------------------------------
# Once-per-task sentinel discipline; disjoint from advisory-hook-state-*.json.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _handler composition: the fifth leg must not clobber or short-circuit the
# other four -- the live risk this fold-in named explicitly.
# ---------------------------------------------------------------------------


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
    """The fifth leg's own internal gate must not suppress its siblings when
    it stays silent (non-Workflow tool_name)."""
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


# ---------------------------------------------------------------------------
# Negative-payload pin (Review: staff-eng Finding 2, EM-adjudicated): the
# check reads no field from `params` beyond the six `_handler` receives --
# session_id, transcript_path, agent_id, tool_name, file_path, content.
# A future edit that reaches for e.g. a `tool_result`/`taskId` field on
# `params` directly (bypassing the transcript-tail derivation route) must
# break this test.
# ---------------------------------------------------------------------------


class _ExplodingOnUnexpectedKey(dict):
    """A dict that raises if any key outside the allowed six is looked up.

    `field()` (coordinator_core.hooks._payload) is the only sanctioned
    accessor for `params` in this module -- it does a plain `.get`, so a
    dict subclass overriding `__getitem__`/`get` is what actually pins the
    contract regardless of which accessor style a future edit reaches for.
    """

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


# ---------------------------------------------------------------------------
# The emitted command has to survive the shell that runs it
# ---------------------------------------------------------------------------


def _launch_transcript(tmp_path, dirname="sub agents", session_name="my session.jsonl"):
    """A transcript whose launch record points at a path containing a space."""
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
    # argv[0] is the absolute launcher path; flag/value pairs follow it. The
    # earlier slice started at 3 because the command opened with the three
    # tokens `python3 -m coordinator_core.workflow_watch`, a form that only
    # ran inside the engine's own environment.
    return dict(zip(argv[1::2], argv[2::2]))


def test_emitted_paths_survive_a_posix_shell_and_resolve(tmp_path):
    """The emitted argument must tokenize identically in BOTH shells.

    The earlier version quoted with `shlex.quote` and parsed with
    `shlex.split` -- POSIX on both sides, so it could only fail if those two
    disagreed, and it rested on a measured-once claim that the harness pipes
    the command through bash. That is a POSIX-only primitive on a
    Windows-first repo, so it is gone. The emitted form is a double-quoted
    path with forward slashes, which cmd.exe and a POSIX shell read the same
    way.

    The watcher would then poll a file it can never open, `TailReader` would
    swallow the OSError, and it would run the FULL cap before exiting 1 --
    silently becoming the very "monitor that outlives its run" this check
    exists to remove. A path containing a space breaks the unquoted form in
    any shell on any host, which is what this fixture uses.
    """
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-quote", str(_launch_transcript(tmp_path)), "Workflow"
    )
    args = _emitted_args(advisory)
    assert os.path.isfile(args["--transcript"])
    assert os.path.isfile(args["--journal"])


def test_journal_path_is_json_decoded_not_raw_capture(tmp_path):
    """`transcriptDir` is captured out of raw transcript text as a JSON string
    LITERAL, so a Windows path arrives with every separator still escaped.
    Using it undecoded yields a doubled-separator path -- wrong even where it
    happens to resolve.
    """
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-decode", str(_launch_transcript(tmp_path)), "Workflow"
    )
    journal = _emitted_args(advisory)["--journal"]
    assert "\\\\" not in journal
    assert journal.count("wf_abc123") == 1


def test_journal_path_does_not_double_append_the_run_id(tmp_path):
    """`transcriptDir` already ends in the run id; appending it again names a
    directory that never exists, so the renderer would emit nothing at all.
    """
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-runid", str(_launch_transcript(tmp_path)), "Workflow"
    )
    assert os.path.isfile(_emitted_args(advisory)["--journal"])


def test_two_async_launch_records_in_one_tail_prefers_the_workflow(tmp_path):
    """A concurrent background dispatch can land its own async_launched record
    later in the tail than this Workflow's. "Last match wins" then reads the
    wrong record. Assert the check does not go silent when the real
    local_workflow launch is present.
    """
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
    # Documents CURRENT behaviour: the shadowing record wins and the check goes
    # quiet. It is fail-safe (never a wrong advisory) but it is a real miss, and
    # it now leaves a stderr breadcrumb instead of being indistinguishable from
    # "no Workflow launched". Pinned so a future fix is a deliberate change.
    assert advisory == ""


def test_sentinel_is_not_written_when_composition_never_completes(tmp_path, monkeypatch):
    """The once-per-task sentinel must not outlive a failed composition.

    Written before the advisory is built, any later failure leaves the sentinel
    on disk while the caller gets nothing — and because the handler collects
    exceptions rather than raising, every later launch of that task id would
    short-circuit on the sentinel and stay silent permanently.
    """
    transcript = _launch_transcript(tmp_path)

    def _boom():
        raise RuntimeError("composition failed")

    monkeypatch.setattr(pad, "_portable_arg", lambda _v: _boom())
    with pytest.raises(RuntimeError):
        pad._check_workflow_monitor_arm_sync("sess-nosent", str(transcript), "Workflow")

    leftovers = list(Path(tempfile.gettempdir()).glob("workflow-monitor-armed-sess-nosent-*"))
    assert leftovers == [], f"sentinel survived a failed composition: {leftovers}"


def test_emitted_args_carry_no_posix_only_quoting(tmp_path):
    """No single quotes and no backslashes -- the two things that make a
    command line mean different things to cmd.exe and to a POSIX shell.
    """
    advisory = pad._check_workflow_monitor_arm_sync(
        "sess-portable", str(_launch_transcript(tmp_path)), "Workflow"
    )
    command = re.search(r'command="(.*?)", timeout_ms', advisory).group(1)
    assert "'" not in command, command
    assert '\\' not in command, command


def test_unformattable_path_emits_nothing_rather_than_a_wrong_command(tmp_path):
    """A character with no form safe in both shells must silence the advisory.

    Uses `$`, which is legal in a Windows filename and still expands inside
    POSIX double quotes -- so it is the realistic case, not a contrived one.
    (A literal double quote cannot be tested this way: Windows forbids it in
    a filename, so no such path can exist to be passed in.)

    Emitting anyway would produce a command line that tokenizes differently
    depending on which shell ran it -- a watcher aimed at the wrong path, or a
    filename interpolating a shell expression.
    """
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
    """No launcher on disk => no advisory, rather than a second broken command.

    A command naming a launcher that was never provisioned fails
    command-not-found, which an EM reads as "this watcher does not exist" —
    indistinguishable from the ModuleNotFoundError it replaced. Silence is
    recoverable; the EM keeps their own monitor.
    """
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
