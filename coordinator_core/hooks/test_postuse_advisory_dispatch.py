
from __future__ import annotations

import asyncio
import builtins
import glob
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402


_TEST_SESSION_STATE_GLOBS = (
    "advisory-hook-state-test-session-*.json",
    ".advisory-hook-state-*.tmp",
    "rt-bark-once-test-session-*",
    "compaction-occurred-test-session-*",
    "compaction-state-test-session-*.md",
    "first-agent-dispatch-advisory-test-session-*",
)


def _sweep_test_session_state_files() -> None:
    tmpdir = tempfile.gettempdir()
    for pattern in _TEST_SESSION_STATE_GLOBS:
        for path in glob.glob(os.path.join(tmpdir, pattern)):
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _reset_advisory_state_files():
    _sweep_test_session_state_files()
    yield
    _sweep_test_session_state_files()


def _bypass_throttle(session_id):
    pad._save_advisory_state(tempfile.gettempdir(), session_id, {"throttle_last_check": 0.0})


def test_throttle_suppresses_second_call_within_window_across_separate_invocations(
    tmp_path, monkeypatch
):
    """The durable 5-minute throttle, proven across two calls that share
    nothing but the state file on disk.

    Invocation 1 needs a signal that the check actually RAN rather than being
    throttled away, so it is given a real sidecar reading in the red band. An
    unmeasured session is silent now, which is indistinguishable from
    throttled — hence the sidecar rather than an absent one.
   
    The reading is a percentage of the 1,000,000-token window named in the
    sidecar block, and the band it lands in is a token runway back from
    `window - 33,000`. 95% is 950,000 tokens, inside the red bound of 897,000.
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW` would move that bound, which is why
    conftest pins it absent suite-wide.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    from coordinator_core.session import context_usage_sidecar as sidecar_module

    sidecar_module._last_written.clear()
    session_id = "test-session-throttle-cross-invocation"
    sidecar_module.write_usage(
        session_id,
        {"used_percentage": 95, "context_window_size": 1_000_000},
        now=time.time(),
    )

    first = pad._check_context_pressure_sync(session_id, "")
    assert "CONTEXT PRESSURE — HANDOFF NOW" in first

    state_path = pad._advisory_state_path(tempfile.gettempdir(), session_id)
    assert os.path.isfile(state_path)
    with open(state_path, encoding="utf-8") as fh:
        persisted = json.load(fh)
    assert persisted["throttle_last_check"] > 0.0

    second = pad._check_context_pressure_sync(session_id, "")
    assert second == ""

def test_throttle_governs_the_orange_band_only_and_never_sits_on_red(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    from coordinator_core.session import context_usage_sidecar as sidecar_module

    sidecar_module._last_written.clear()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"model": "claude-sonnet-4-5-20250929"}) + "\n")

    def _seed(session_id, used_percentage):
        sidecar_module.write_usage(
            session_id,
            {"used_percentage": used_percentage, "context_window_size": 1_000_000},
            now=time.time(),
        )
        pad._save_advisory_state(
            tempfile.gettempdir(), session_id, {"throttle_last_check": time.time()}
        )

    orange_session = "test-session-throttle-isolated-orange"
    _seed(orange_session, 88)
    assert pad._check_context_pressure_sync(orange_session, str(transcript)) == ""

    red_session = "test-session-throttle-isolated-red"
    _seed(red_session, 95)
    red = pad._check_context_pressure_sync(red_session, str(transcript))
    assert "CONTEXT PRESSURE" in red
    assert "~95% of window used" in red

    pad._save_advisory_state(
        tempfile.gettempdir(),
        red_session,
        dict(
            pad._load_advisory_state(tempfile.gettempdir(), red_session),
            throttle_last_check=0.0,
        ),
    )
    assert pad._check_context_pressure_sync(red_session, str(transcript)) == ""


def test_compaction_advisory_fires_exactly_once_per_sentinel_and_rearms(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("small transcript content\n" * 5)
    post_size = transcript.stat().st_size
    pre_size = post_size * 10

    session_id = "test-session-compaction-once"
    tmpdir = tempfile.gettempdir()
    sentinel = os.path.join(tmpdir, f"compaction-occurred-{session_id}")
    state_snapshot = os.path.join(tmpdir, f"compaction-state-{session_id}.md")

    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write(str(pre_size))
    with open(state_snapshot, "w", encoding="utf-8") as fh:
        fh.write("first snapshot")

    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" in first
    assert "first snapshot" in first
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)

    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" not in second

    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write(str(pre_size))
    with open(state_snapshot, "w", encoding="utf-8") as fh:
        fh.write("second snapshot")

    third = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" in third
    assert "second snapshot" in third
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)


def test_first_agent_dispatch_fires_once_on_first_agent_call():
    session_id = "test-session-first-agent-dispatch-fires"

    from coordinator_core.session.machinery_paths import SHARE_RELDIR

    first = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert first != ""
    assert session_id in first
    assert f"{SHARE_RELDIR}/" in first

    second = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert second == ""


def test_first_agent_dispatch_silent_for_non_agent_tool_even_on_first_call():
    session_id = "test-session-first-agent-dispatch-non-agent"

    for tool_name in ("Bash", "Read", "Explore", "general-purpose", ""):
        assert pad._check_first_agent_dispatch_sync(session_id, tool_name) == ""

    fired = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert fired != ""


def test_first_agent_dispatch_silent_when_session_id_absent():
    assert pad._check_first_agent_dispatch_sync("", "Agent") == ""


def test_first_agent_dispatch_sentinel_write_failure_degrades_to_silence(monkeypatch):
    session_id = "test-session-first-agent-dispatch-write-fail"

    def _raise(*args, **kwargs):
        raise OSError("simulated sentinel-write failure")

    monkeypatch.setattr(pad, "open", _raise, raising=False)

    result = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert result == ""

    tmpdir = tempfile.gettempdir()
    sentinel = pad._first_agent_dispatch_sentinel_path(tmpdir, session_id)
    assert not os.path.isfile(sentinel)


def test_first_agent_dispatch_sentinel_partial_write_failure_allows_retry(monkeypatch):
    session_id = "test-session-first-agent-dispatch-partial-write"

    real_open = builtins.open
    tmpdir = tempfile.gettempdir()
    sentinel = pad._first_agent_dispatch_sentinel_path(tmpdir, session_id)

    class _FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            raise OSError("simulated mid-write failure")

    def _fake_open(path, mode="r", *args, **kwargs):
        if str(path) == sentinel and mode == "w":
            real_open(path, "w", encoding=kwargs.get("encoding", "utf-8")).close()
            return _FailingFile()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(pad, "open", _fake_open, raising=False)

    result = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert result == ""
    assert not os.path.isfile(sentinel)

    monkeypatch.setattr(pad, "open", real_open, raising=False)
    retried = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert retried != ""
    assert os.path.isfile(sentinel)


def test_handler_first_agent_dispatch_composes_with_existing_advisories():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-three-way-merge"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Agent"})
            )

    hso = result["hookSpecificOutput"]
    context = hso["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "COORDINATOR SIDECAR ADVISORY" in context
    assert session_id in context
    assert "\n\n" in context

    assert context.index("cp text") < context.index("rt text") < context.index(
        "COORDINATOR SIDECAR ADVISORY"
    )


def test_handler_first_agent_dispatch_alone_still_post_advisory():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-agent-only"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Agent"})
            )

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "COORDINATOR SIDECAR ADVISORY" in hso["additionalContext"]


def test_handler_existing_advisories_unaffected_by_non_agent_tool_name():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-non-agent-cp-still-fires"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Bash"})
            )

    hso = result["hookSpecificOutput"]
    assert hso["additionalContext"].endswith("cp text")
    assert "COORDINATOR SIDECAR ADVISORY" not in hso["additionalContext"]


def _handoff_write_params(session_id, **overrides):
    params = {
        "session_id": session_id,
        "tool_name": "Write",
        "file_path": "state/handoffs/2026-08-06_120000_some-topic.md",
        "content": "---\ntitle: something\n---\n",
    }
    params.update(overrides)
    return params


def test_handler_unauthorized_handoff_nudge_fires_on_handoff_write():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-fires"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(pad._handler(_handoff_write_params(session_id)))

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "[nudge]" in hso["additionalContext"]
    assert "state/handoffs" in hso["additionalContext"]


def test_handler_unauthorized_handoff_silent_for_ordinary_write():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-ordinary-write"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler(
                    _handoff_write_params(
                        session_id, file_path="coordinator_core/hooks/whatever.py"
                    )
                )
            )

    assert result == {}


def test_handler_unauthorized_handoff_merges_last_after_existing_advisories():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-four-way-merge"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            result = asyncio.run(pad._handler(_handoff_write_params(session_id)))

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("cp text") < context.index("rt text") < context.index("[nudge]")


def test_handler_unauthorized_handoff_survives_absent_session_id():
    import asyncio

    result = asyncio.run(pad._handler(_handoff_write_params("")))

    assert "[nudge]" in result["hookSpecificOutput"]["additionalContext"]


def test_handler_unauthorized_handoff_silent_when_stub_omits_file_path():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-unplumbed-stub"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler({"session_id": session_id, "tool_name": "Write"})
            )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.endswith("cp text")
    assert "[nudge]" not in context


def test_handler_unauthorized_handoff_respects_kind_recovery_suppression():
    import asyncio
    import unittest.mock as mock

    session_id = "test-session-handler-uh-recovery-suppressed"

    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            result = asyncio.run(
                pad._handler(
                    _handoff_write_params(
                        session_id, content="---\nkind: recovery\n---\n"
                    )
                )
            )

    assert result == {}


def test_runtime_tripwire_is_off_unless_explicitly_armed(tmp_path, monkeypatch):
    from coordinator_core.git import repo_root as repo_root_seam

    monkeypatch.delenv("COORDINATOR_RUNTIME_TRIPWIRE", raising=False)

    consulted = []

    def _tripwire_should_not_reach_here(cwd=None):
        consulted.append(cwd)
        return str(tmp_path)

    monkeypatch.setattr(
        repo_root_seam, "show_toplevel", _tripwire_should_not_reach_here
    )

    assert pad._check_runtime_tripwire_sync("test-session-rt-default-off", "") == ""
    assert not consulted, "unarmed tripwire did work before checking its gate"


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "1 "])
def test_runtime_tripwire_arms_only_on_exactly_one(value, tmp_path, monkeypatch):
    from coordinator_core.git import repo_root as repo_root_seam

    monkeypatch.setenv("COORDINATOR_RUNTIME_TRIPWIRE", value)
    consulted = []
    monkeypatch.setattr(
        repo_root_seam,
        "show_toplevel",
        lambda cwd=None: (consulted.append(cwd), str(tmp_path))[1],
    )

    assert pad._check_runtime_tripwire_sync("test-session-rt-arm-strict", "") == ""
    assert not consulted


def test_runtime_tripwire_resolves_repo_root_via_seam_not_a_spawn(monkeypatch):
    import subprocess as _subprocess

    from coordinator_core.git import repo_root as repo_root_seam

    monkeypatch.setenv("COORDINATOR_RUNTIME_TRIPWIRE", "1")

    spawned = []

    def _fail_on_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError(f"unexpected subprocess spawn: {args!r}")

    monkeypatch.setattr(_subprocess, "run", _fail_on_spawn)

    calls = []

    def _fake_show_toplevel(cwd=None):
        calls.append(cwd)
        return None

    monkeypatch.setattr(repo_root_seam, "show_toplevel", _fake_show_toplevel)

    assert pad._check_runtime_tripwire_sync("test-session-rt-seam", "") == ""
    assert calls, "show_toplevel was not consulted"
    assert not spawned, f"unexpected subprocess spawn: {spawned!r}"


def test_runtime_tripwire_happy_path_resolves_through_seam_and_fires(
    tmp_path, monkeypatch
):
    from coordinator_core.git import repo_root as repo_root_seam

    monkeypatch.setenv("COORDINATOR_RUNTIME_TRIPWIRE", "1")

    git_root = tmp_path
    session_id = "test-session-rt-happy-path"
    em_sid = "test-session-rt-happy-path-em"

    agent_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / session_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")

    em_dir = git_root / ".git" / "coordinator-sessions" / em_sid
    em_dir.mkdir(parents=True)
    dispatched_at = int(time.time()) - 999_999
    (em_dir / "dispatched-agents.txt").write_text(
        f"{session_id}\tclaude-sonnet-4-5\tgeneral-purpose\t{dispatched_at}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        repo_root_seam, "show_toplevel", lambda cwd=None: str(git_root)
    )

    isolated_tmpdir = tmp_path / "rt-tripwire-tmpdir"
    isolated_tmpdir.mkdir()
    monkeypatch.setattr(pad, "_tempfile", lambda: type(
        "_Shim", (), {"gettempdir": staticmethod(lambda: str(isolated_tmpdir))}
    )())

    result = pad._check_runtime_tripwire_sync(session_id, "")

    assert result != ""
    assert "RUNTIME TRIPWIRE" in result
    assert "stop starting new work" in result

    second = pad._check_runtime_tripwire_sync(session_id, "")
    assert second == ""


def test_runtime_tripwire_fails_open_when_seam_raises(monkeypatch):
    from coordinator_core.git import repo_root as repo_root_seam

    def _boom(cwd=None):
        raise OSError("cwd vanished")

    monkeypatch.setattr(repo_root_seam, "show_toplevel", _boom)

    assert pad._check_runtime_tripwire_sync("test-session-rt-seam-raises", "") == ""

# This op replaced four separate hook PROCESSES, and a process boundary


def _raise(exc):
    def _boom(*_args, **_kwargs):
        raise exc

    return _boom


def test_one_raising_leg_does_not_suppress_its_siblings(monkeypatch, capsys):
    monkeypatch.setattr(
        pad, "_check_context_pressure_sync", _raise(OSError("transcript unreadable"))
    )
    monkeypatch.setattr(pad, "_check_runtime_tripwire_sync", lambda *a: "tripwire spoke")
    monkeypatch.setattr(pad, "_check_first_agent_dispatch_sync", lambda *a: "")

    async def _quiet(*_a, **_k):
        return ""

    monkeypatch.setattr(pad.nudge_unauthorized_handoff, "advisory_text", _quiet)

    result = asyncio.run(
        pad._handler(
            {"session_id": "test-session-isolation", "tool_name": "Agent"},
            repo_root=None,
        )
    )

    assert "tripwire spoke" in result["hookSpecificOutput"]["additionalContext"]
    stderr = capsys.readouterr().err
    assert "leg=context_pressure" in stderr
    assert "OSError" in stderr


def test_every_leg_raising_still_returns_a_clean_no_advisory(monkeypatch):
    for name in (
        "_check_context_pressure_sync",
        "_check_runtime_tripwire_sync",
        "_check_first_agent_dispatch_sync",
    ):
        monkeypatch.setattr(pad, name, _raise(RuntimeError("disk gone")))

    async def _also_raises(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(pad.nudge_unauthorized_handoff, "advisory_text", _also_raises)

    result = asyncio.run(
        pad._handler(
            {"session_id": "test-session-isolation-all", "tool_name": "Agent"},
            repo_root=None,
        )
    )

    assert result == {}


def test_the_session_id_absent_short_circuit_also_fails_open(monkeypatch, capsys):

    async def _raises(*_a, **_k):
        raise OSError("transcript unreadable")

    monkeypatch.setattr(pad.nudge_unauthorized_handoff, "advisory_text", _raises)

    result = asyncio.run(pad._handler({"tool_name": "Write"}, repo_root=None))

    assert result == {}
    assert "leg=unauthorized_handoff" in capsys.readouterr().err
