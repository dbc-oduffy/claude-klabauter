"""A first-seen agy subagent conversation id costs under the 500 ms process-time bar and no bounded_io stall."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _advisory_dedupe, dispatch

LIVE_CONVERSATION = "d8f5a804-4fe2-40a9-a267-0c7e7c9929f3"
SHAPE_KEY = "guard__0123456789abcdef"
PROCESS_BAR_SECS = 0.5


def _advisory_envelope() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "advisory prose"}}


def test_first_seen_conversation_marks_and_reads_without_a_stall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _advisory_dedupe.already_advised(tmp_path, LIVE_CONVERSATION, SHAPE_KEY) is False
    started = time.process_time()
    _advisory_dedupe.mark_advised(tmp_path, LIVE_CONVERSATION, SHAPE_KEY)
    process_secs = time.process_time() - started

    assert "bounded_io: stalled" not in capsys.readouterr().err
    assert process_secs < PROCESS_BAR_SECS
    assert _advisory_dedupe.already_advised(tmp_path, LIVE_CONVERSATION, SHAPE_KEY) is True


def test_dispatch_dedupe_consult_on_a_first_seen_conversation_waits_on_no_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)
    out = _advisory_envelope()

    started = time.process_time()
    first = dispatch._session_advisory_already_fired("guard", out, LIVE_CONVERSATION, str(tmp_path))
    process_secs = time.process_time() - started
    second = dispatch._session_advisory_already_fired("guard", out, LIVE_CONVERSATION, str(tmp_path))

    assert (first, second) == (False, True)
    assert process_secs < PROCESS_BAR_SECS
    assert "bounded_io: stalled" not in capsys.readouterr().err


def test_a_slow_sweep_does_not_drop_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_advisory_dedupe, "_FS_DEADLINE_SECS", 0.05)

    def _slow_sweep(gitdir: Path, session_id: str) -> int:
        time.sleep(0.3)
        return 0

    monkeypatch.setattr(_advisory_dedupe, "_sweep_stale_session_dirs", _slow_sweep)

    _advisory_dedupe.mark_advised(tmp_path, LIVE_CONVERSATION, SHAPE_KEY)

    assert "advisory-dedupe-sweep" in capsys.readouterr().err
    assert _advisory_dedupe.already_advised(tmp_path, LIVE_CONVERSATION, SHAPE_KEY) is True


def test_a_sweep_that_overruns_is_stamped_so_the_next_session_does_not_repeat_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _failing_sweep(gitdir: Path, session_id: str) -> int:
        calls.append(1)
        raise RuntimeError("abandoned mid-sweep")

    monkeypatch.setattr(_advisory_dedupe, "_sweep_stale_session_dirs", _failing_sweep)

    _advisory_dedupe.mark_advised(tmp_path, "session-one", SHAPE_KEY)
    sentinel = tmp_path / "advisory-dedupe" / _advisory_dedupe._LAST_SWEEP_SENTINEL
    assert sentinel.exists()
    _advisory_dedupe.mark_advised(tmp_path, "session-two", SHAPE_KEY)

    assert calls == [1]


def test_a_stale_sentinel_still_sweeps_and_reaps_an_old_sibling(tmp_path: Path) -> None:
    _advisory_dedupe.mark_advised(tmp_path, "session-one", SHAPE_KEY)
    root = tmp_path / "advisory-dedupe"
    old_dir = root / "old-session"
    old_dir.mkdir()
    (old_dir / SHAPE_KEY).touch()
    ancient = time.time() - 60 * 60 * 60
    os.utime(old_dir / SHAPE_KEY, (ancient, ancient))
    os.utime(old_dir, (ancient, ancient))
    stale = time.time() - 60 * 60
    os.utime(root / _advisory_dedupe._LAST_SWEEP_SENTINEL, (stale, stale))

    _advisory_dedupe.mark_advised(tmp_path, "session-two", SHAPE_KEY)

    assert not old_dir.exists()
    assert (root / "session-two" / SHAPE_KEY).exists()
