"""Pins `render.py`'s (C2) rendering contract: one line per journal event,
never more; hard truncation of oversized `result` values; and — the
regression that matters — no code path ever emits a raw journal line
(chunk C6, docs/plans/2026-08-30-the-workflow-monitor-outlives-the-run-it-watches.md).

Negative-spec: does NOT exercise `terminal.py`'s transcript matching (that
is `test_terminal_detection.py`'s job) and does NOT assert on journal
balance (`started == result + failed`) as any kind of signal — `render.py`
never counts events, only renders them, per its own negative-spec block.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workflow_watch.render import (
    RESULT_TRUNCATE_BYTES,
    JournalRenderer,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _append(path, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def _write_meta(run_dir, agent_id, agent_type="general-purpose", model="claude-sonnet-5"):
    meta_path = run_dir / f"agent-{agent_id}.meta.json"
    meta_path.write_text(
        json.dumps({"agentType": agent_type, "model": model}), encoding="utf-8"
    )


def _event(agent_id, event_type, **extra):
    payload = {"agentId": agent_id, "type": event_type}
    payload.update(extra)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# One line per event, never more


def test_one_line_per_event(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1


def test_repolling_unchanged_journal_never_reemits(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    first = renderer.poll()
    assert len(first) == 1

    # No bytes appended — TailReader's buffer still contains the same
    # event; the seen-set must not re-emit it.
    second = renderer.poll()
    assert second == []


def test_growth_only_emits_the_new_event(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    first = renderer.poll()
    assert len(first) == 1

    _append(journal, _event("a1", "result", result="done") + "\n")
    second = renderer.poll()
    assert len(second) == 1
    assert "result" in second[0]


# ---------------------------------------------------------------------------
# started line names agentType and model from agent-<id>.meta.json


def test_started_line_names_agent_type_and_model(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1", agent_type="executor", model="claude-opus-5")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "executor" in lines[0]
    assert "claude-opus-5" in lines[0]


def test_started_line_survives_absent_meta_file(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "unknown-agent" in lines[0]
    assert "unknown-model" in lines[0]


def test_started_line_survives_malformed_meta_file(tmp_path):
    journal = tmp_path / "journal.jsonl"
    meta_path = tmp_path / "agent-a1.meta.json"
    meta_path.write_text("{not valid json", encoding="utf-8")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "unknown-agent" in lines[0]
    assert "unknown-model" in lines[0]


# ---------------------------------------------------------------------------
# failed line is unmistakably marked


def test_failed_line_is_unmistakably_marked(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "failed") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "FAILED" in lines[0]


# ---------------------------------------------------------------------------
# Multi-KB result is truncated to the documented cap


def test_result_is_truncated_to_documented_cap(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    huge_result = "x" * (RESULT_TRUNCATE_BYTES * 4)
    _write(journal, _event("a1", "result", result=huge_result) + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    line = lines[0]
    prefix = "result   general-purpose: "
    payload = line[len(prefix):]
    assert len(payload.encode("utf-8")) <= RESULT_TRUNCATE_BYTES
    assert "…[truncated]" in line
    # Never the raw, untruncated payload.
    assert huge_result not in line


def test_small_result_is_not_truncated(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "result", result="short and sweet") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "short and sweet" in lines[0]
    assert "…[truncated]" not in lines[0]


# ---------------------------------------------------------------------------
# No code path ever emits a raw journal line — the regression that matters


def test_no_raw_journal_line_ever_emitted(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    raw_line = _event("a1", "started")
    _write(journal, raw_line + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    # The rendered line must never equal or contain the raw JSON text.
    assert lines[0] != raw_line
    assert raw_line not in lines[0]
    assert "{" not in lines[0]
    assert "}" not in lines[0]


def test_unrecognised_event_type_never_falls_back_to_raw_json(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "some-future-event-type") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    # Silence, not a guess and never a raw dump.
    assert lines == []


def test_malformed_json_line_never_leaks_into_output(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, "{not valid json\n" + _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    lines = renderer.poll()
    assert len(lines) == 1
    assert "not valid json" not in lines[0]


# ---------------------------------------------------------------------------
# Shrink-reset: journal shrinks between polls; the seen-set (not the byte
# offset) prevents duplicate emission when tail.py resets to re-scan.


def test_journal_shrink_does_not_reemit_already_rendered_event(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write_meta(tmp_path, "a2")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    first = renderer.poll()
    assert len(first) == 1

    # Journal shrinks and is rewritten smaller than the reader's offset —
    # tail.py resets its offset to 0 and re-scans from the start. The same
    # a1/started event reappears in the rewritten content alongside a new
    # event; only the new event should be emitted.
    _write(
        journal,
        _event("a1", "started") + "\n" + _event("a2", "started") + "\n",
    )
    second = renderer.poll()
    # Only the new (a2) event is emitted; a1's already-rendered event is
    # not re-emitted a second time.
    assert len(second) == 1
    combined = first + second
    assert len(combined) == 2


def test_journal_shrink_to_empty_then_regrowth_does_not_duplicate(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_meta(tmp_path, "a1")
    _write(journal, _event("a1", "started") + "\n")

    renderer = JournalRenderer(str(journal))
    assert len(renderer.poll()) == 1

    # Shrink to empty, then rewrite with the exact same event.
    _write(journal, "")
    assert renderer.poll() == []

    _write(journal, _event("a1", "started") + "\n")
    assert renderer.poll() == []


# ---------------------------------------------------------------------------
# The documented entry point, exercised as a process
# ---------------------------------------------------------------------------


def _run_module(tmp_path, task_id, transcript_text, cap="2", poll="0.2"):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(transcript_text, encoding="utf-8")
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable, "-m", "coordinator_core.workflow_watch",
            "--transcript", str(transcript),
            "--journal", str(journal),
            "--task-id", task_id,
            "--poll-interval", poll,
            "--cap", cap,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[2].parent),
        **no_console_creationflags(),
    )
    return proc, time.monotonic() - started


def test_module_is_executable_via_dash_m(tmp_path):
    """`python3 -m coordinator_core.workflow_watch` must actually start.

    Every other test in this package calls `main`/`_watch` directly, so all of
    them passed while the package had no `__main__.py` and the exact command
    the PostToolUse advisory hands the EM died with "cannot be directly
    executed" before polling once. Import-level coverage cannot see that; only
    running it as a process can.
    """
    proc, _ = _run_module(tmp_path, "never-ends", '{"noise":1}\n')
    assert "cannot be directly executed" not in proc.stderr
    assert "No module named" not in proc.stderr


def test_cap_is_self_enforced_and_exits_nonzero(tmp_path):
    """The cap is the watcher's own bound, not the caller's.

    Nothing external stops this process: no terminal record ever appears and
    no Monitor `timeout_ms` is in play. It must still exit on its own, and
    non-zero, so a consumer distinguishes "gave up" from "the run ended"
    without parsing stdout.
    """
    proc, elapsed = _run_module(tmp_path, "never-ends", '{"noise":1}\n', cap="2")
    assert proc.returncode == 1
    assert elapsed >= 2.0
    assert "cap reached" in proc.stderr


@pytest.mark.parametrize("status", ["completed", "failed", "killed", "stopped"])
def test_terminal_record_exits_zero_for_every_status(tmp_path, status):
    """All four statuses are real detections, so all four exit 0.

    A detector that collapsed `failed`/`killed` into the give-up exit code
    would be silent through exactly the runs an EM most needs to hear about.
    """
    transcript = (
        '{"noise":1}\n'
        f"<task-notification><task-id>ends-now</task-id>"
        f"<status>{status}</status></task-notification>\n"
    )
    proc, elapsed = _run_module(tmp_path, "ends-now", transcript, cap="30")
    assert proc.returncode == 0
    assert f"terminal: {status}" in proc.stdout
    assert elapsed < 30


# ---------------------------------------------------------------------------
# The reader must not hand the same bytes back twice
# ---------------------------------------------------------------------------


def test_poll_lines_returns_only_the_delta(tmp_path):
    """Re-delivery is invisible when a seen-set hides it, so pin it directly.

    `poll()` returns its whole bounded buffer every call by design, for the
    terminal matcher's straddle window. A line-oriented consumer polling that
    once a second re-parses the same bytes for the life of the run. Only the
    seen-set made it look correct.
    """
    from coordinator_core.workflow_watch.tail import TailReader

    journal = tmp_path / "j.jsonl"
    journal.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    reader = TailReader(str(journal))

    assert reader.poll_lines() == ['{"a":1}', '{"a":2}']
    assert reader.poll_lines() == []

    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"a":3}\n')
    assert reader.poll_lines() == ['{"a":3}']
    assert reader.poll_lines() == []


def test_poll_lines_holds_a_partial_line_until_its_newline(tmp_path):
    """A JSONL writer can be mid-line when we read. Emitting the fragment
    would hand the parser a truncated record; holding it costs one poll.
    """
    from coordinator_core.workflow_watch.tail import TailReader

    journal = tmp_path / "j.jsonl"
    journal.write_text('{"a":1}\n{"par', encoding="utf-8")
    reader = TailReader(str(journal))
    assert reader.poll_lines() == ['{"a":1}']

    with journal.open("a", encoding="utf-8") as handle:
        handle.write('tial":true}\n')
    assert reader.poll_lines() == ['{"partial":true}']


def test_renderer_does_not_re_render_a_quiet_journal(tmp_path):
    """The end-to-end property: polling a journal that has not grown emits
    nothing and does no parsing work.
    """
    from coordinator_core.workflow_watch.render import JournalRenderer

    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"type": "started", "agentId": "a1"}) + "\n", encoding="utf-8"
    )
    renderer = JournalRenderer(str(journal))
    first = renderer.poll()
    assert len(first) == 1
    for _ in range(5):
        assert renderer.poll() == []


def test_multibyte_character_split_across_a_read_boundary_survives(tmp_path):
    """A UTF-8 sequence straddling two reads must not be corrupted.

    Decoding each chunk independently replaces the leading bytes with U+FFFD
    on the spot; the rest of the sequence then arrives orphaned, so the
    character is lost permanently rather than merely late. A live-appended
    journal splits mid-character routinely — an agent name, a model id, or any
    non-ASCII text inside a result blob is enough.
    """
    from coordinator_core.workflow_watch.tail import TailReader

    journal = tmp_path / "j.jsonl"
    blob = '{"a":"\u00e9\U0001f600"}\n'.encode("utf-8")
    split = len(blob) // 2
    journal.write_bytes(blob[:split])

    reader = TailReader(str(journal))
    reader.poll_lines()
    with journal.open("ab") as handle:
        handle.write(blob[split:])

    assert reader.poll_lines() == ['{"a":"\u00e9\U0001f600"}']


def test_buffered_reader_also_survives_a_split_multibyte_character(tmp_path):
    """Same property for `poll()`, which the terminal matcher reads."""
    from coordinator_core.workflow_watch.tail import TailReader

    transcript = tmp_path / "t.jsonl"
    blob = 'caf\u00e9 \U0001f600 done\n'.encode("utf-8")
    split = len(blob) // 2
    transcript.write_bytes(blob[:split])

    reader = TailReader(str(transcript))
    reader.poll()
    with transcript.open("ab") as handle:
        handle.write(blob[split:])

    assert "\ufffd" not in reader.poll()
