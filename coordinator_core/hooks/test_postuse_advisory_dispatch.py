"""
coordinator_core.hooks.test_postuse_advisory_dispatch -- tests for the non-context-
pressure advisory paths in postuse_advisory_dispatch.py: the post-compaction sentinel
bridge, the durable throttle state (round-tripped across separate process
invocations), first-agent-dispatch, unauthorized-handoff, and the runtime tripwire.

The sidecar-sourced context-pressure measurement path (`_check_context_pressure_sync`'s
Phase 2) is covered by its own test file, not here:
coordinator_core/hooks/tests/test_postuse_context_pressure.py. The transcript-scan
measurement machinery this file used to cover -- `_extract_last_usage_tokens`,
`_resolve_context_window`, `_check_unrecognised_sonnet_generation`, and the byte-based
proxy fallback -- is deleted; see docs/plans/2026-08-17-the-advisory-reads-the-harness.md.

Spec backlink: coordinator_core/hooks/postuse_advisory_dispatch.py (module under test).
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Shared fixture: clean up durable per-session state files between tests.
#
# All tests below use session ids prefixed "test-session-" (by convention) so
# this fixture can sweep them without touching real session state. The state
# is now file-backed (see postuse_advisory_dispatch.py's module-level comment
# above _advisory_state_path for why the former in-memory dicts this fixture
# used to clear were the bug, not the fix).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _check_context_pressure_sync integration-style tests
#
# The transcript-scan measurement path (_extract_last_usage_tokens,
# _resolve_context_window, _check_unrecognised_sonnet_generation, and the
# byte-based proxy fallback) is deleted; its coverage is superseded by the
# sidecar-sourced measurement path's own test file, not duplicated here:
# coordinator_core/hooks/tests/test_postuse_context_pressure.py.
# ---------------------------------------------------------------------------


def _bypass_throttle(session_id):
    # Force the 5-min throttle to be considered "expired" for this session --
    # writes the durable state file directly, since that (not process memory)
    # is now the source of truth.
    pad._save_advisory_state(tempfile.gettempdir(), session_id, {"throttle_last_check": 0.0})


def test_check_context_pressure_fallback_bark_once_per_transcript(tmp_path):
    """The fail-loud disclosure fires at most once per transcript_hash, same
    dedup discipline as the token-accurate path -- not on every throttle-gated
    call for the life of a long, usage-block-less session."""
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    context_window = 200_000
    bytes_per_token = 7
    critical_pct = 50
    critical_bytes = context_window * critical_pct * bytes_per_token // 100
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    lines = [model_line]
    needed_lines = (critical_bytes // len(filler_line)) + 10
    lines.extend([filler_line] * needed_lines)
    transcript.write_text("\n".join(lines) + "\n")

    session_id = "test-session-fallback-bark-once"
    _bypass_throttle(session_id)
    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "CONTEXT PRESSURE — UNKNOWN" in first

    # Re-arm ONLY the throttle timestamp -- `_bypass_throttle` overwrites the
    # whole state file, which would also wipe the critical_fired/advisory_fired
    # dedup this test is specifically checking survives a throttle re-arm.
    tmpdir = tempfile.gettempdir()
    state = pad._load_advisory_state(tmpdir, session_id)
    state["throttle_last_check"] = 0.0
    pad._save_advisory_state(tmpdir, session_id, state)

    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert second == ""


# ---------------------------------------------------------------------------
# Durable-state regression tests.
#
# These cover the actual bug: this op is dispatched via a FRESH process per
# PostToolUse fire (no resident daemon, DR-215), so any guard kept only in a
# module-level dict/set re-initializes empty on every call and never
# suppresses anything. None of these tests share Python-object state between
# calls -- there is none any more, by construction -- so a call "seeing" a
# prior call's effect here is proof the durable state FILE (not process
# memory) is what's doing the suppressing, faithfully simulating what a
# second, wholly separate process invocation would observe.
# ---------------------------------------------------------------------------


def test_throttle_suppresses_second_call_within_window_across_separate_invocations(
    tmp_path,
):
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    user_line = json.dumps({"type": "user", "message": {"content": "hi"}})
    transcript.write_text("\n".join([model_line, user_line]) + "\n")

    session_id = "test-session-throttle-cross-invocation"

    # "Invocation" 1: no prior state on disk anywhere for this session, so the
    # throttle does NOT suppress -- the real check runs and persists
    # throttle_last_check as a side effect. No sidecar is present in tmp_path,
    # so the sidecar-sourced path reports UNKNOWN rather than "" -- that is
    # the measurement outcome, not a throttle outcome; what this test isolates
    # is that the check ran at all (as opposed to being throttled away).
    first = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "CONTEXT PRESSURE — UNKNOWN" in first

    state_path = pad._advisory_state_path(tempfile.gettempdir(), session_id)
    assert os.path.isfile(state_path)
    with open(state_path, encoding="utf-8") as fh:
        persisted = json.load(fh)
    assert persisted["throttle_last_check"] > 0.0

    # "Invocation" 2: same session, well within the 5-minute throttle window.
    # Nothing in this test process hands state from call 1 to call 2 directly
    # -- only the file on disk does.
    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert second == ""


def test_throttle_suppresses_even_when_content_would_otherwise_fire(tmp_path):
    """Isolates the throttle guard specifically (not bark-once): pre-seed
    throttle_last_check to "just now" for a session that has NEVER fired
    before, then confirm a critical-sized transcript is still suppressed."""
    transcript = tmp_path / "transcript.jsonl"
    model_line = json.dumps({"model": "claude-sonnet-4-5-20250929"})
    context_window = 200_000
    bytes_per_token = 7
    critical_pct = 50
    critical_bytes = context_window * critical_pct * bytes_per_token // 100
    filler_line = json.dumps({"type": "user", "message": {"content": "x" * 500}})
    lines = [model_line]
    needed_lines = (critical_bytes // len(filler_line)) + 10
    lines.extend([filler_line] * needed_lines)
    transcript.write_text("\n".join(lines) + "\n")
    assert transcript.stat().st_size >= critical_bytes

    session_id = "test-session-throttle-isolated"
    pad._save_advisory_state(
        tempfile.gettempdir(), session_id, {"throttle_last_check": time.time()}
    )

    result = pad._check_context_pressure_sync(session_id, str(transcript))

    assert result == ""  # throttled despite content that would otherwise fire critical


def test_compaction_advisory_fires_exactly_once_per_sentinel_and_rearms(tmp_path):
    """Covers the most severe instance of the regression: the sentinel used to
    never be deleted (B-F1) and the in-memory consumption marker never
    survived the fresh-process-per-fire model, so the advisory re-fired on
    every subsequent call for the rest of the session. Also proves consumption
    is per-EVENT (delete-on-read), not an eternal per-session flag -- a second,
    later compaction in the same session must fire again."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("small transcript content\n" * 5)
    post_size = transcript.stat().st_size
    pre_size = post_size * 10  # comfortably satisfies the 85%-shrink real-compaction guard

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
    # Consumed by delete -- the once-only firing guard for THIS event.
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)

    # Second call, same session, no new sentinel written: must NOT re-fire.
    second = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" not in second

    # A later compaction event in the same long session re-arms correctly.
    with open(sentinel, "w", encoding="utf-8") as fh:
        fh.write(str(pre_size))
    with open(state_snapshot, "w", encoding="utf-8") as fh:
        fh.write("second snapshot")

    third = pad._check_context_pressure_sync(session_id, str(transcript))
    assert "COMPACTION OCCURRED" in third
    assert "second snapshot" in third
    assert not os.path.isfile(sentinel)
    assert not os.path.isfile(state_snapshot)


# ---------------------------------------------------------------------------
# _check_first_agent_dispatch_sync unit tests.
#
# One-time-per-session advisory telling the EM that coordinator subagents write
# full findings to an on-disk sidecar. Gated on tool_name == "Agent" (not a
# subagent_type prefix -- the payload this op receives carries no
# subagent_type field) plus a durable once-per-session sentinel.
# ---------------------------------------------------------------------------


def test_first_agent_dispatch_fires_once_on_first_agent_call():
    session_id = "test-session-first-agent-dispatch-fires"

    first = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert first != ""
    assert session_id in first
    assert "state/subagent-share/" in first

    # Second Agent-tool call, same session -- must not re-fire.
    second = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert second == ""


def test_first_agent_dispatch_silent_for_non_agent_tool_even_on_first_call():
    session_id = "test-session-first-agent-dispatch-non-agent"

    for tool_name in ("Bash", "Read", "Explore", "general-purpose", ""):
        assert pad._check_first_agent_dispatch_sync(session_id, tool_name) == ""

    # No sentinel written by a non-Agent tool_name -- a later real Agent
    # dispatch in the same session must still fire.
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
    """Review: code-reviewer (Finding 3) -- if open() succeeds but write()
    raises mid-write (e.g. disk full), the sentinel file already exists on
    disk. Without cleanup, every later call in the session would see the
    partial file and stay silent forever. The failed write must remove the
    sentinel so a later Agent dispatch in the same session can retry."""
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
            # Mirrors open() succeeding (the file lands on disk) then
            # write() raising mid-write.
            real_open(path, "w", encoding=kwargs.get("encoding", "utf-8")).close()
            return _FailingFile()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(pad, "open", _fake_open, raising=False)

    result = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert result == ""
    assert not os.path.isfile(sentinel)

    # Retry (real open() now, monkeypatch still active but path differs after
    # the sentinel was removed -- same code path, no partial file to trip on):
    monkeypatch.setattr(pad, "open", real_open, raising=False)
    retried = pad._check_first_agent_dispatch_sync(session_id, "Agent")
    assert retried != ""
    assert os.path.isfile(sentinel)


# ---------------------------------------------------------------------------
# _handler integration: composition with the other two checks (the regression
# that matters most -- the new third check must never clobber or short-circuit
# the existing context-pressure / runtime-tripwire advisories).
# ---------------------------------------------------------------------------


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

    # Review: code-reviewer (Finding 4) -- membership alone doesn't pin the
    # merge order the commit message claims (cp -> rt -> first-agent-dispatch);
    # a future reorder of the join would pass the assertions above unnoticed.
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


# ---------------------------------------------------------------------------
# Fourth check: the unauthorized-handoff nudge folded in from DoE's separate
# PostToolUse(Write) registration (cross-repo memo
# 2026-08-06-doe-claude-em-postuse-fold-nudge-unauthorized-handoff.md).
# ---------------------------------------------------------------------------


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
    """The nudge's predicate is the Write payload alone — the session_id
    short-circuit that silences the other three must not swallow it."""
    import asyncio

    result = asyncio.run(pad._handler(_handoff_write_params("")))

    assert "[nudge]" in result["hookSpecificOutput"]["additionalContext"]


def test_handler_unauthorized_handoff_silent_when_stub_omits_file_path():
    """DoE's dispatcher stub must map tool_input.file_path/content into params.
    Until it does, the fourth check stays silent and the other three still fire."""
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


# ---------------------------------------------------------------------------
# _check_runtime_tripwire_sync — repo-root resolution goes through the shared
# memoized seam (coordinator_core.git.repo_root), never a per-tool-call
# `git rev-parse --show-toplevel` spawn. This check runs from an EMPTY-matcher
# PostToolUse hook, so a spawn here is one process per tool call.
# ---------------------------------------------------------------------------


def test_runtime_tripwire_resolves_repo_root_via_seam_not_a_spawn(monkeypatch):
    # Review: code-reviewer (P2, W2) -- `_fail_on_spawn` raising AssertionError
    # is itself an Exception, and every spawn site it could intercept lives
    # inside `_check_runtime_tripwire_sync`'s own `except Exception: return ""`,
    # so a bare `assert result == ""` still passes against the OLD (spawning)
    # implementation -- the guard could never fail. Record the spawn attempt in
    # a list BEFORE raising, and assert against that list after the call, so
    # the evidence survives the swallow and the test genuinely discriminates
    # old (spawns) vs. new (walks) behavior.
    import subprocess as _subprocess

    from coordinator_core.git import repo_root as repo_root_seam

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
    """Review: code-reviewer (P3, W4) -- both existing seam tests stub
    show_toplevel to None/raise, so only the earliest early-exit
    (`if not git_root: return ""`) is ever driven. This test resolves a real
    root through the seam and continues into the agents-dir / back-pointer /
    dispatch-record / threshold logic, covering the path that actually
    changed."""
    from coordinator_core.git import repo_root as repo_root_seam

    git_root = tmp_path
    session_id = "test-session-rt-happy-path"
    em_sid = "test-session-rt-happy-path-em"

    agent_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / session_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")

    em_dir = git_root / ".git" / "coordinator-sessions" / em_sid
    em_dir.mkdir(parents=True)
    dispatched_at = int(time.time()) - 999_999  # comfortably past every threshold
    (em_dir / "dispatched-agents.txt").write_text(
        f"{session_id}\tclaude-sonnet-4-5\tgeneral-purpose\t{dispatched_at}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        repo_root_seam, "show_toplevel", lambda cwd=None: str(git_root)
    )

    # Review: code-reviewer (F6) -- this test's bark-once sentinel
    # (rt-bark-once-{session_id}) lives under the REAL tempfile.gettempdir(),
    # the same directory this module's autouse _sweep_test_session_state_files
    # globs before/after every test in the file. Under xdist (--dist load
    # splits one file across workers), a peer worker's sweep can land in the
    # microsecond window between this test's two calls and unlink the
    # sentinel, flipping `second` from "" to a fire -- a latent flake, not
    # something to paper over with timing. Route this test's tempdir through a
    # tmp_path-backed shim instead, so its sentinel lives somewhere no other
    # worker's glob ever reaches.
    isolated_tmpdir = tmp_path / "rt-tripwire-tmpdir"
    isolated_tmpdir.mkdir()
    monkeypatch.setattr(pad, "_tempfile", lambda: type(
        "_Shim", (), {"gettempdir": staticmethod(lambda: str(isolated_tmpdir))}
    )())

    result = pad._check_runtime_tripwire_sync(session_id, "")

    assert result != ""
    assert "RUNTIME TRIPWIRE" in result
    assert "stop starting new work" in result

    # Bark-once sentinel now present -- second call for the same session_id
    # must not re-fire.
    second = pad._check_runtime_tripwire_sync(session_id, "")
    assert second == ""


def test_runtime_tripwire_fails_open_when_seam_raises(monkeypatch):
    """Contract test, not a change test -- Review: code-reviewer (P3, W3): this
    also passes against the pre-image (the stub is never consulted there; real
    git resolves and the function early-exits at the agents_dir check). It pins
    the never-raises contract going forward, it does not discriminate the
    repo-root-via-seam change itself -- see
    test_runtime_tripwire_resolves_repo_root_via_seam_not_a_spawn for that."""
    from coordinator_core.git import repo_root as repo_root_seam

    def _boom(cwd=None):
        raise OSError("cwd vanished")

    monkeypatch.setattr(repo_root_seam, "show_toplevel", _boom)

    assert pad._check_runtime_tripwire_sync("test-session-rt-seam-raises", "") == ""
