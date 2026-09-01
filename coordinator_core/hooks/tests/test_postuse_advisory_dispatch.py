"""
coordinator_core.hooks.tests.test_postuse_advisory_dispatch — tests for the
sixth PostToolUse advisory leg folded into `postuse_advisory_dispatch.py`:
`_check_group_em_watch_arm_sync` and its composition inside `_handler`.

Covers: the universal (no tool_name) gate; the Group-EM check (no session_id, no
git root, no nomination record, record naming a different session); the
never-armed transcript scan (armed marker present/absent, unreadable
transcript); the emitted advisory shape (`persistent=true`, never
`persistent=false`); the once-per-session sentinel discipline (disjoint from
`advisory-hook-state-{session_id}.json`); the missing-launcher fail-open path
(this leg's launcher is never installed today -- see the module-level
comment above `_check_group_em_watch_arm_sync`); and the `_handler`
six-way merge (the sixth leg must not clobber or short-circuit the other
five).

Spec backlink: coordinator_core/hooks/postuse_advisory_dispatch.py
`_check_group_em_watch_arm_sync` (module under test); C10,
docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md
"""

from __future__ import annotations

import asyncio
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
from coordinator_core.group_em import nomination as group_em_nomination  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point the module under test at a per-test temp dir instead of the box's.

    Same rationale as test_postuse_workflow_monitor_arm.py's own fixture:
    every sentinel/state path in this module is derived from
    `tempfile.gettempdir()`, so redirecting it isolates every test by
    construction.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield


@pytest.fixture
def _group_em_repo(tmp_path, monkeypatch):
    """Make `SESSION` read as the Group EM Group-EM holder for a fresh repo root.

    Patches the git-root seam this leg reuses from `_check_runtime_tripwire_sync`
    (`coordinator_core.git.repo_root.show_toplevel`) and writes a real nomination
    record via `group_em.nomination.claim` -- the same writer `groupem.enter`
    itself calls -- rather than hand-building the JSON shape, so a future record
    shape change fails this fixture instead of silently drifting from it.
    """
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    nomination_dir = tmp_path / "group-em-records"
    nomination_dir.mkdir()
    group_em_nomination.claim(repo_root, SESSION, directory=nomination_dir)
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel", lambda: repo_root
    )
    _original_read_record = group_em_nomination.read_record
    monkeypatch.setattr(
        group_em_nomination,
        "read_record",
        lambda root, directory=None: _original_read_record(root, directory=nomination_dir),
    )
    return repo_root


@pytest.fixture
def _installed_group_em_watch_launcher(tmp_path, monkeypatch):
    """Provision a settings-home `bin/group-em-watch` for the module under test.

    `_check_group_em_watch_arm_sync` names the watcher by its ABSOLUTE
    installed launcher path and stays SILENT when that launcher is not on
    disk (see the module-level comment above the function under test: no such
    launcher has actually been generated anywhere in this repo yet), so
    without this fixture every emission test would assert against "" for
    that reason alone rather than the one under test.
    `test_stays_silent_when_no_launcher_is_installed` opts back out.
    """
    bin_dir = tmp_path / "settings-home" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "group-em-watch").write_text("stub launcher", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    yield str(bin_dir / "group-em-watch")


SESSION = "test-session-group-em-watch"


def _write_transcript(tmp_path, *lines, name="transcript.jsonl"):
    transcript = tmp_path / name
    transcript.write_text("".join(lines), encoding="utf-8")
    return str(transcript)


# ---------------------------------------------------------------------------
# Universal gate: no session_id => silent, no tool_name dependency at all.
# ---------------------------------------------------------------------------


def test_silent_without_session_id(tmp_path):
    transcript_path = _write_transcript(tmp_path, "irrelevant content\n")
    assert pad._check_group_em_watch_arm_sync("", transcript_path) == ""


# ---------------------------------------------------------------------------
# Group-EM check.
# ---------------------------------------------------------------------------


def test_silent_when_no_git_root(tmp_path, monkeypatch, _installed_group_em_watch_launcher):
    # Review: review-integrator (finding #1, EM-ratified P1) -- without the
    # launcher fixture this test short-circuited on the launcher probe
    # before ever reaching the git-root check it is named for.
    monkeypatch.setattr("coordinator_core.git.repo_root.show_toplevel", lambda: "")
    transcript_path = _write_transcript(tmp_path, "irrelevant content\n")
    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


def test_silent_when_git_seam_raises(tmp_path, monkeypatch, _installed_group_em_watch_launcher):
    # Review: review-integrator (finding #1, EM-ratified P1) -- see
    # test_silent_when_no_git_root above.
    def _boom():
        raise RuntimeError("git absent")

    monkeypatch.setattr("coordinator_core.git.repo_root.show_toplevel", _boom)
    transcript_path = _write_transcript(tmp_path, "irrelevant content\n")
    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


def test_silent_when_no_nomination_record(tmp_path, monkeypatch, _installed_group_em_watch_launcher):
    # Review: review-integrator (finding #1, EM-ratified P1) -- see
    # test_silent_when_no_git_root above.
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel", lambda: repo_root
    )
    transcript_path = _write_transcript(tmp_path, "irrelevant content\n")
    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


def test_silent_when_group_em_held_by_another_session(tmp_path, monkeypatch, _installed_group_em_watch_launcher):
    # Review: review-integrator (finding #1, EM-ratified P1) -- see
    # test_silent_when_no_git_root above.
    repo_root = str(tmp_path / "repo")
    os.makedirs(repo_root, exist_ok=True)
    nomination_dir = tmp_path / "group-em-records"
    nomination_dir.mkdir()
    group_em_nomination.claim(repo_root, "someone-else-session", directory=nomination_dir)
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel", lambda: repo_root
    )
    _original_read_record = group_em_nomination.read_record
    monkeypatch.setattr(
        group_em_nomination,
        "read_record",
        lambda root, directory=None: _original_read_record(root, directory=nomination_dir),
    )
    transcript_path = _write_transcript(tmp_path, "irrelevant content\n")
    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


# ---------------------------------------------------------------------------
# Never-armed transcript scan.
# ---------------------------------------------------------------------------


def test_fires_when_group_em_never_armed_and_launcher_installed(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "some ordinary transcript content\n")

    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    assert result != ""
    assert "GROUP EM WATCH" in result


def test_silent_when_armed_marker_already_present(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(
        tmp_path, "ARMED peer_count=3 claude-klabauter peers, snapshot=1.2ms, interval=5.0s\n"
    )

    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    assert result == ""


def test_returns_empty_not_raises_on_unreadable_transcript(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    # Review: review-integrator (finding #1, EM-ratified P1) -- without the
    # launcher fixture this test short-circuited on the launcher probe
    # before ever reaching the transcript-read logic it is named for.
    missing_path = os.path.join(tempfile.gettempdir(), "does-not-exist-group-em-watch.jsonl")
    assert not os.path.isfile(missing_path)

    result = pad._check_group_em_watch_arm_sync(SESSION, missing_path)

    assert result == ""


def test_silent_without_transcript_path(tmp_path, _group_em_repo, _installed_group_em_watch_launcher):
    # Review: review-integrator (finding #1, EM-ratified P1) -- see
    # test_returns_empty_not_raises_on_unreadable_transcript above.
    assert pad._check_group_em_watch_arm_sync(SESSION, "") == ""


# ---------------------------------------------------------------------------
# Emitted advisory shape: persistent=true, never persistent=false.
# ---------------------------------------------------------------------------


def test_advisory_names_persistent_true_never_false(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")

    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    assert result != ""
    assert "persistent=true" in result
    assert "persistent=false" not in result


# ---------------------------------------------------------------------------
# Once-per-session sentinel discipline; disjoint from advisory-hook-state-*.json.
# ---------------------------------------------------------------------------


def test_fires_once_per_session_via_sentinel(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")

    first = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)
    assert first != ""

    second = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)
    assert second == ""

    sentinel = pad._group_em_watch_arm_sentinel_path(tempfile.gettempdir(), SESSION)
    assert os.path.isfile(sentinel)


def test_never_touches_the_shared_advisory_hook_state_file(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    shared_state_path = pad._advisory_state_path(tempfile.gettempdir(), SESSION)
    assert not os.path.isfile(shared_state_path)

    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    assert result != ""
    assert not os.path.isfile(shared_state_path)


# ---------------------------------------------------------------------------
# Missing-launcher fail-open path (the current, always-true state of this
# repo -- see the module-level comment above the function under test).
# ---------------------------------------------------------------------------


def test_stays_silent_when_no_launcher_is_installed(tmp_path, _group_em_repo, monkeypatch):
    empty_home = tmp_path / "empty-settings-home"
    (empty_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_home))

    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


def test_sentinel_is_not_written_when_the_launcher_is_missing(
    tmp_path, _group_em_repo, monkeypatch
):
    empty_home = tmp_path / "empty-settings-home-2"
    (empty_home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(empty_home))

    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    sentinel = pad._group_em_watch_arm_sentinel_path(tempfile.gettempdir(), SESSION)
    assert not os.path.isfile(sentinel)


def test_the_launcher_probe_resolves_nothing_from_an_empty_settings_home(tmp_path, monkeypatch):
    """The probe answers the disk, and stays None when the launcher is absent.

    This test used to assert the same thing about the REAL settings home, on
    the reasoning that no `group-em-watch` launcher had been generated
    anywhere yet. That premise expired: `coordinator/bin/group-em-watch.py`
    landed 2026-09-01 (117dbd53c2) and `install.substrate` enumerates
    `coordinator/bin/*.py` into the settings home under its stem, so the
    launcher this leg names now exists and installs like any other. Pinning
    the machine's install state made this test assert that a shipped artifact
    can never arrive -- it would have started failing on the next install with
    nothing wrong. What is worth pinning is the FAIL-OPEN rule itself: a leg
    that cannot find the launcher composes no command at all, because a
    command naming an entrypoint that will not run is worse than silence.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "empty-home"))
    assert pad._group_em_watch_launcher() is None


# ---------------------------------------------------------------------------
# The emitted command survives the shell that runs it (reuses _portable_arg).
# ---------------------------------------------------------------------------


def _emitted_command(advisory):
    return re.search(r'command="(.*?)", persistent=true', advisory).group(1)


def test_emitted_args_carry_no_posix_only_quoting(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)
    command = _emitted_command(result)
    assert "'" not in command, command
    assert "\\" not in command, command


def test_command_names_the_installed_launcher_not_a_bare_dash_m(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    result = pad._check_group_em_watch_arm_sync(SESSION, transcript_path)
    command = _emitted_command(result)
    argv = shlex.split(command)

    assert "python3" not in command
    assert "-m" not in argv
    assert argv[0] == _installed_group_em_watch_launcher.replace("\\", "/")
    assert os.path.isabs(argv[0])


def test_unformattable_repo_root_emits_nothing_rather_than_a_wrong_command(
    tmp_path, monkeypatch, _installed_group_em_watch_launcher
):
    repo_root = str(tmp_path / "we$rd repo")
    os.makedirs(repo_root, exist_ok=True)
    nomination_dir = tmp_path / "group-em-records"
    nomination_dir.mkdir()
    group_em_nomination.claim(repo_root, SESSION, directory=nomination_dir)
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel", lambda: repo_root
    )
    _original_read_record = group_em_nomination.read_record
    monkeypatch.setattr(
        group_em_nomination,
        "read_record",
        lambda root, directory=None: _original_read_record(root, directory=nomination_dir),
    )
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")

    assert pad._check_group_em_watch_arm_sync(SESSION, transcript_path) == ""


def test_sentinel_is_not_written_when_composition_never_completes(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher, monkeypatch
):
    """The once-per-session sentinel must not outlive a failed composition."""
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")

    def _boom(_v):
        raise RuntimeError("composition failed")

    monkeypatch.setattr(pad, "_portable_arg", _boom)
    with pytest.raises(RuntimeError):
        pad._check_group_em_watch_arm_sync(SESSION, transcript_path)

    sentinel = pad._group_em_watch_arm_sentinel_path(tempfile.gettempdir(), SESSION)
    assert not os.path.isfile(sentinel)


# ---------------------------------------------------------------------------
# _handler composition: the sixth leg must not clobber or short-circuit the
# other five.
# ---------------------------------------------------------------------------


def test_handler_six_way_merge_all_legs_fire_in_fixed_order(tmp_path):
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
                    with mock.patch.object(
                        pad, "_check_workflow_monitor_arm_sync", return_value="wf text"
                    ):
                        with mock.patch.object(
                            pad,
                            "_check_group_em_watch_arm_sync",
                            return_value="ge text",
                        ):
                            result = asyncio.run(
                                pad._handler(
                                    {"session_id": SESSION, "tool_name": "Bash"}
                                )
                            )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "agent text" in context
    assert "[nudge] text" in context
    assert "wf text" in context
    assert "ge text" in context

    assert (
        context.index("cp text")
        < context.index("rt text")
        < context.index("agent text")
        < context.index("[nudge] text")
        < context.index("wf text")
        < context.index("ge text")
    )


def test_handler_other_five_legs_fire_unaffected_when_group_em_leg_silent(tmp_path):
    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            with mock.patch.object(
                pad, "_check_first_agent_dispatch_sync", return_value=""
            ):
                with mock.patch.object(
                    pad, "_check_group_em_watch_arm_sync", return_value=""
                ):
                    result = asyncio.run(
                        pad._handler({"session_id": SESSION, "tool_name": "Bash"})
                    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "cp text" in context
    assert "rt text" in context
    assert "GROUP EM WATCH" not in context


def test_handler_group_em_watch_alone_still_post_advisory(tmp_path):
    with mock.patch.object(pad, "_check_context_pressure_sync", return_value=""):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value=""):
            with mock.patch.object(
                pad, "_check_first_agent_dispatch_sync", return_value=""
            ):
                with mock.patch.object(
                    pad,
                    "_check_group_em_watch_arm_sync",
                    return_value="GROUP EM WATCH: arm it",
                ):
                    result = asyncio.run(
                        pad._handler({"session_id": SESSION, "tool_name": "Bash"})
                    )

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "GROUP EM WATCH" in hso["additionalContext"]


# ---------------------------------------------------------------------------
# Negative-payload pin: the check reads no field from `params` beyond the
# six `_handler` receives.
# ---------------------------------------------------------------------------


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


def test_handler_reads_no_params_field_beyond_the_six_mapped_fields(
    tmp_path, _group_em_repo, _installed_group_em_watch_launcher
):
    transcript_path = _write_transcript(tmp_path, "no armed marker here\n")
    params = _ExplodingOnUnexpectedKey(
        {
            "session_id": SESSION,
            "transcript_path": transcript_path,
            "agent_id": "",
            "tool_name": "Bash",
            "file_path": "",
            "content": "",
        }
    )

    result = asyncio.run(pad._handler(params))

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "GROUP EM WATCH" in context
