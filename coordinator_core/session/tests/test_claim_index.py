"""Tests for coordinator_core.session.claim_index.

Plan: docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md,
chunk C1. Every fixture here is a synthetic session/agent dir tree built
under ``tmp_path`` — no process is spawned, and the real
``.git/coordinator-sessions/`` is never touched (every call passes
``sessions_dir=str(tmp_path)`` explicitly).

The two exceptions are C8's ``TestAC18RebuildAtCorpusCWidth`` (docs/plans/
2026-08-25-the-touched-files-record-gets-a-designed-shape.md), which spawn
real ``sys.executable`` driver processes through
``benchmarks.process_time.batched_process_time_ms`` to get a real
process-time/spawn-count figure for ``rebuild()`` at Corpus C width — each
is marked ``spawns_process`` and ``cadence`` per this repo's spawn ratchet
(``coordinator_core/tests/test_no_new_spawning_tests.py``).
"""

import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.session import claim_index, touch_record


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _touch_line(verb, path, when="2026-08-08T10:00:00.000000Z"):
    return f"{verb} {when} {path}"


def _append_fixture_line(sink, session_id, agent_id, line):
    """Parse one `_touch_line`-produced OLD-dialect string
    (``'<verb> <iso8601> <path>'``) and append it as a NEW-dialect
    ``touch-record.jsonl`` event via ``touch_record.append_event``.

    C7b: this module's reader no longer reads ``touched.txt`` at all (the
    AC21 transitional union-read and its enumeration arm are deleted), so
    every fixture writer in this file must emit the seam's own dialect.
    Parsing `_touch_line`'s pre-existing textual shape here (rather than
    reworking every call site below) keeps every test body byte-identical
    to before this chunk landed.
    """
    verb, ts_str, path = line.split(None, 2)
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    touch_record.append_event(
        sink, session_id=session_id, agent_id=agent_id, verb=verb, path=path, timestamp=ts
    )


def _session_touched(base, sid, lines):
    sink = os.path.join(str(base), sid, "touch-record.jsonl")
    for line in lines:
        _append_fixture_line(sink, sid, None, line)


def _append_named_fixture_line(sink, session_id, agent_id, line, name):
    """Same as ``_append_fixture_line``, but pins an explicit ``name`` on
    the event (C2, docs/plans/2026-09-01-the-claim-record-carries-the-
    name.md) rather than letting ``append_event`` resolve one off the test
    process's own (absent) harness registry record."""
    verb, ts_str, path = line.split(None, 2)
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    touch_record.append_event(
        sink,
        session_id=session_id,
        agent_id=agent_id,
        verb=verb,
        path=path,
        timestamp=ts,
        name=name,
    )


def _session_touched_named(base, sid, lines_and_names):
    """Like ``_session_touched``, but each entry is a ``(line, name)`` pair
    so a fixture can pin a per-event recorded name."""
    sink = os.path.join(str(base), sid, "touch-record.jsonl")
    for line, name in lines_and_names:
        _append_named_fixture_line(sink, sid, None, line, name)


def _agent_touched(base, agent_id, owner_sid, lines):
    agent_dir = os.path.join(str(base), ".agents", agent_id)
    sink = os.path.join(agent_dir, "touch-record.jsonl")
    for line in lines:
        _append_fixture_line(sink, owner_sid, agent_id, line)
    _write(os.path.join(agent_dir, "em-session-id.txt"), owner_sid + "\n")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_claimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo/bar.py")])

    result = claim_index.lookup(["foo/bar.py"], sessions_dir=base)

    assert result == {"foo/bar.py": ["sess-a"]}
    # Unconditional rebuild: a second, independent lookup() call re-derives
    # the same answer from the substrate, not from any persisted cache.
    result_again = claim_index.lookup(["foo/bar.py"], sessions_dir=base)
    assert result_again == {"foo/bar.py": ["sess-a"]}


def test_round_trip_unclaimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo/bar.py")])

    result = claim_index.lookup(["never/touched.py"], sessions_dir=base)

    assert result == {"never/touched.py": []}


def test_rebuild_returns_in_memory_state_only(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is True
    assert state.claims == {"foo.py": ["sess-a"]}
    # No persisted index file is written anywhere under the sessions dir.
    for _root, dirs, _files in os.walk(base):
        assert ".index" not in dirs


# ---------------------------------------------------------------------------
# Torn-line tolerance
# ---------------------------------------------------------------------------


def test_torn_trailing_line_in_touch_record_is_discarded(tmp_path):
    base = str(tmp_path)
    # A complete claim line, then a torn (no trailing newline) fragment
    # simulating a reader that caught a concurrent writer mid-append --
    # C7b: exercised against the NEW `touch-record.jsonl` dialect, the only
    # one this module reads any more.
    complete_line = touch_record.encode_line(
        session_id="sess-a", agent_id=None, verb="T", path="complete.py",
        timestamp=1723107600.0,
    ).decode("utf-8")
    torn = (
        complete_line
        + '{"v":1,"verb":"T","ts":1723107601.0,"sid":"sess-a",'
        '"agent":null,"path":"partial-wr"'
    )
    _write(os.path.join(base, "sess-a", "touch-record.jsonl"), torn)

    result = claim_index.lookup(["complete.py", "partial-wr"], sessions_dir=base)

    assert result["complete.py"] == ["sess-a"]
    assert result["partial-wr"] == []  # torn line never resolved -> not claimed


# ---------------------------------------------------------------------------
# unconditional rebuild -- lookup() must see an append to an EXISTING
# claimant's touched.txt, not just a brand-new session dir landing
# ---------------------------------------------------------------------------


def test_lookup_sees_second_claim_appended_to_existing_session(tmp_path):
    """C1b regression: appending a 2nd claim to an ALREADY-EXISTING
    session's touched.txt (what ``scope.py::touch`` does on every call
    after a session's first) changes that file's mtime, not any
    directory's. Under the deleted mtime-staleness probe this test would
    FAIL, because neither the top-level sessions-dir nor ``sess-a/``'s own
    mtime moves on an in-place append -- lookup() would keep serving the
    cached pre-append snapshot and report ``other.py`` unclaimed. Under
    unconditional rebuild it passes, because every lookup() call re-walks
    the substrate regardless of any mtime signal.
    """
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    first = claim_index.lookup(["other.py"], sessions_dir=base)
    assert first == {"other.py": []}

    # Organic append: same session dir, same file, no os.utime anywhere --
    # exactly what scope.py::touch does for a 2nd-and-later claim.
    touched_path = os.path.join(base, "sess-a", "touch-record.jsonl")
    touch_record.append_event(
        touched_path, session_id="sess-a", agent_id=None, verb="T", path="other.py"
    )

    second = claim_index.lookup(["other.py"], sessions_dir=base)
    assert second == {"other.py": ["sess-a"]}


def test_lookup_unresolvable_sessions_dir_is_unanswerable(tmp_path):
    result = claim_index.lookup(["foo.py"], sessions_dir="")
    assert result == {"foo.py": [claim_index.UNANSWERABLE]}
    # C1 (docs/plans/2026-08-11-claim-index-abort-cause-and-cli-blindness.md,
    # AC6) -- the empty-base cause is reported as structured data, not just
    # membership. AC2 above still holds: membership alone is untouched.
    assert result.abort_cause == claim_index.ABORT_CAUSE_EMPTY_BASE


def test_lookup_missing_sessions_dir_on_disk_is_unclaimed_not_unanswerable(tmp_path):
    missing = os.path.join(str(tmp_path), "does-not-exist-yet")
    result = claim_index.lookup(["foo.py"], sessions_dir=missing)
    assert result == {"foo.py": []}
    # A genuinely-absent directory is an honest empty, never an abort (C1
    # AC6) -- must not be mislabelled with any of the three abort causes.
    assert result.complete is True
    assert result.abort_cause is None


# ---------------------------------------------------------------------------
# Review: coordinator:code-reviewer P1 -- an I/O error reading a claim
# source (as opposed to that source genuinely not existing) must surface as
# UNANSWERABLE, never silently collapse to "unclaimed" -- that is the one
# answer that authorizes a write.
# ---------------------------------------------------------------------------


def test_lookup_permission_error_scanning_sessions_dir_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(base):
            raise PermissionError("simulated I/O error")
        return real_scandir(path)

    monkeypatch.setattr(claim_index.os, "scandir", fake_scandir)

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": [claim_index.UNANSWERABLE]}
    assert result.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR


def test_lookup_permission_error_scanning_agents_subdir_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    agents_base = os.path.join(base, claim_index._AGENTS_SUBDIR)

    real_scandir = os.scandir

    def fake_scandir(path):
        if os.path.abspath(str(path)) == os.path.abspath(agents_base):
            raise PermissionError("simulated I/O error")
        return real_scandir(path)

    monkeypatch.setattr(claim_index.os, "scandir", fake_scandir)

    # A path resolvable from the session-dir scan still resolves fine; the
    # incompleteness only bites paths this walk never actually reached.
    result = claim_index.lookup(["foo.py", "never/touched.py"], sessions_dir=base)

    assert result["foo.py"] == ["sess-a"]
    assert result["never/touched.py"] == [claim_index.UNANSWERABLE]
    assert result.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR


def test_lookup_permission_error_reading_agent_backpointer_is_unanswerable(
    tmp_path, monkeypatch
):
    base = str(tmp_path)
    _agent_touched(base, "agent-1", "sess-owner", [_touch_line("T", "x.py")])
    backptr = os.path.join(base, ".agents", "agent-1", "em-session-id.txt")

    real_open = open

    def fake_open(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(backptr):
            raise PermissionError("simulated I/O error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    result = claim_index.lookup(["x.py"], sessions_dir=base)

    assert result == {"x.py": [claim_index.UNANSWERABLE]}
    assert result.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR


def test_lookup_permission_error_reading_touch_record_body_is_unanswerable(
    tmp_path, monkeypatch
):
    """Bug backlog
    2026-08-11-an-io-error-reading-a-touched-txt-body-i-18abf7c6f3be (P3):
    an OSError raised while reading a claimant's record CONTENT (as opposed
    to enumerating it) must surface as UNANSWERABLE for the path that
    claimant would have claimed, never silently collapse to "unclaimed" --
    that is the one answer that authorizes a write over a live peer.

    C7b: ``touch_record._read_stream_claims`` reads via
    ``pathlib.Path.read_bytes``, not ``builtins.open`` -- patched
    accordingly (the pre-C7b version of this test patched ``open``, which
    is why it targeted ``touched.txt``, read by this module's own retired
    line-oriented reader)."""
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    touched_path = os.path.join(base, "sess-a", "touch-record.jsonl")

    from pathlib import Path

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self):
        if os.path.abspath(str(self)) == os.path.abspath(touched_path):
            raise PermissionError("simulated I/O error reading body")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": [claim_index.UNANSWERABLE]}
    assert result.complete is False
    assert result.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR


def test_torn_tail_content_read_still_reports_complete(tmp_path):
    """Regression guard on invariant 1: a torn (mid-append) trailing line is
    a normal read outcome, not an IO error -- the walk must still report
    complete=True, abort_cause=None."""
    base = str(tmp_path)
    complete_line = touch_record.encode_line(
        session_id="sess-a", agent_id=None, verb="T", path="complete.py",
        timestamp=1723107600.0,
    ).decode("utf-8")
    torn = (
        complete_line
        + '{"v":1,"verb":"T","ts":1723107601.0,"sid":"sess-a",'
        '"agent":null,"path":"partial"'
    )
    _write(os.path.join(base, "sess-a", "touch-record.jsonl"), torn)

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is True
    assert state.abort_cause is None


def test_empty_touch_record_still_reports_complete(tmp_path):
    """Regression guard on invariant 2: a genuinely empty record file is not
    an IO error."""
    base = str(tmp_path)
    _write(os.path.join(base, "sess-a", "touch-record.jsonl"), "")

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is True
    assert state.abort_cause is None
    assert state.claims == {}


def test_missing_touch_record_still_reports_complete(tmp_path):
    """Regression guard on invariant 3: FileNotFoundError reading a
    claimant's record content is not an IO error -- it means no claims yet
    from that claimant, not a substrate failure."""
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "sess-a"), exist_ok=True)
    touched_path = os.path.join(base, "sess-a", "touch-record.jsonl")
    # Create then remove so _enumerate_claim_sinks sees it as a file at
    # enumeration time but the content read below hits FileNotFoundError.
    _write(touched_path, "")
    os.remove(touched_path)

    # C4 retired `_read_lines_discard_torn_tail`; the property it pinned now
    # lives on the seam read -- an absent file is "no claims from this
    # claimant", never a substrate failure.
    claims, read_ok = claim_index._read_stream_claims(touched_path)

    assert claims == {}
    assert read_ok is True


def test_enumeration_io_error_wins_over_later_content_read_error(tmp_path, monkeypatch):
    """First-detected-cause-wins: an enumeration-time IO error (agent
    backpointer unreadable) must not be overwritten by a later content-read
    IO error encountered further along the same walk."""
    base = str(tmp_path)
    _agent_touched(base, "agent-1", "sess-owner", [_touch_line("T", "x.py")])
    backptr = os.path.join(base, ".agents", "agent-1", "em-session-id.txt")
    _session_touched(base, "sess-z", [_touch_line("T", "y.py")])
    content_path = os.path.join(base, "sess-z", "touch-record.jsonl")

    real_open = open

    def fake_open(path, *args, **kwargs):
        p = os.path.abspath(str(path))
        if p == os.path.abspath(backptr):
            raise PermissionError("simulated enumeration-time I/O error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    from pathlib import Path

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self):
        if os.path.abspath(str(self)) == os.path.abspath(content_path):
            raise PermissionError("simulated content-read I/O error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is False
    assert state.abort_cause == claim_index.ABORT_CAUSE_IO_ERROR


# ---------------------------------------------------------------------------
# Process-time cap -- the module's other surviving degradation route
# ---------------------------------------------------------------------------


def test_rebuild_cap_exceeded_mid_walk_marks_incomplete(tmp_path, monkeypatch):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    # Force the very first cap check inside the walk to already be past
    # deadline, deterministically, without depending on real wall-clock
    # timing (a real 500ms sleep would be a needless fixture cost).
    monkeypatch.setattr(claim_index, "REBUILD_PROCESS_TIME_CAP_SECS", -1.0)

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is False
    assert state.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED


def test_lookup_cap_exceeded_resolves_unanswerable_not_unclaimed(tmp_path, monkeypatch):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    monkeypatch.setattr(claim_index, "REBUILD_PROCESS_TIME_CAP_SECS", -1.0)

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": [claim_index.UNANSWERABLE]}
    assert result.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED


def test_rebuild_cap_exceeded_after_one_file_reports_cap_exceeded_not_empty_base(
    tmp_path, monkeypatch
):
    """Drives the cap-exceeded abort by patching ``time.process_time`` (per the
    worked construction in
    ``state/subagent-share/31106a01-e326-4ab8-a033-b4aa5d757cbd/
    repro-partial-positive-fail-open.py``), NOT by zeroing
    ``REBUILD_PROCESS_TIME_CAP_SECS`` — a zeroed cap breaks before the first
    read and yields an empty claims dict, which would look identical to the
    empty-base case (C1 AC6) if this test asserted only ``claims == {}``.
    This construction consumes ``sess-a``'s file before the deadline check
    trips, proving the walk genuinely started rather than never running."""
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    _session_touched(base, "sess-b", [_touch_line("T", "bar.py")])

    # sess-a sorts first; consume it, then trip the deadline before sess-b.
    ticks = iter([0.0, 0.0, 10_000.0] + [10_000.0] * 64)
    # AC18 re-keyed the cap from wall clock to process time; same
    # construction, driven through the instrument the module now reads.
    monkeypatch.setattr(claim_index.time, "process_time", lambda: next(ticks))

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is False
    assert state.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED
    assert state.claims == {"foo.py": ["sess-a"]}


# ---------------------------------------------------------------------------
# Both agent-dir and session-dir are read
# ---------------------------------------------------------------------------


def test_agent_dir_claim_attributed_to_owner_session(tmp_path):
    base = str(tmp_path)
    _agent_touched(
        base, "agent-123", "sess-owner", [_touch_line("T", "engine/core.py")]
    )

    result = claim_index.lookup(["engine/core.py"], sessions_dir=base)

    assert result == {"engine/core.py": ["sess-owner"]}


def test_session_dir_and_agent_dir_claims_both_read_in_one_rebuild(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "session_owned.py")])
    _agent_touched(base, "agent-1", "sess-b", [_touch_line("T", "agent_owned.py")])

    result = claim_index.lookup(
        ["session_owned.py", "agent_owned.py"], sessions_dir=base
    )

    assert result == {
        "session_owned.py": ["sess-a"],
        "agent_owned.py": ["sess-b"],
    }


def test_agent_dir_with_no_backpointer_contributes_no_claims(tmp_path):
    base = str(tmp_path)
    agent_dir = os.path.join(base, ".agents", "orphan-agent")
    _write(
        os.path.join(agent_dir, "touched.txt"), _touch_line("T", "orphan.py") + "\n"
    )
    # No em-session-id.txt written -- unresolvable owner.

    result = claim_index.lookup(["orphan.py"], sessions_dir=base)

    assert result == {"orphan.py": []}


# ---------------------------------------------------------------------------
# Released-then-reclaimed round trip
# ---------------------------------------------------------------------------


def test_claim_then_release_resolves_unclaimed(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
        ],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": []}


def test_claim_release_reclaim_resolves_to_reclaimant(tmp_path):
    base = str(tmp_path)
    # sess-a claims then releases doc.md; sess-b claims it afterward. The
    # negative spec this guards: a T-only reader would still see sess-a's
    # T and wrongly report it claimed by the releasing session.
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
        ],
    )
    _session_touched(
        base,
        "sess-b",
        [_touch_line("T", "doc.md", when="2026-08-08T10:06:00.000000Z")],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": ["sess-b"]}


def test_same_session_reclaims_after_releasing(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "doc.md", when="2026-08-08T10:00:00.000000Z"),
            _touch_line("R", "doc.md", when="2026-08-08T10:05:00.000000Z"),
            _touch_line("T", "doc.md", when="2026-08-08T10:06:00.000000Z"),
        ],
    )

    result = claim_index.lookup(["doc.md"], sessions_dir=base)

    assert result == {"doc.md": ["sess-a"]}


# ---------------------------------------------------------------------------
# R1 -- a backslashed caller pathspec must not read as unclaimed. Keys
# parsed from touched.txt are POSIX-separated (scope.py's write side); the
# production caller (_check_claim_conflicts in scoped_git_commit.py) may
# pass a backslashed relative pathspec on Windows, this repo's first-class
# platform. A raw string-equality miss there authorizes a write against a
# live claim purely on separator dialect.
# ---------------------------------------------------------------------------


def test_lookup_backslashed_input_resolves_claimed_posix_key(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a\\b\\c.py"], sessions_dir=base)

    assert result == {"a\\b\\c.py": ["sess-a"]}


def test_lookup_forward_slashed_input_resolves_backslashed_key(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a\\b\\c.py")])

    result = claim_index.lookup(["a/b/c.py"], sessions_dir=base)

    assert result == {"a/b/c.py": ["sess-a"]}


def test_lookup_mixed_separator_input_resolves_claimed(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a\\b/c.py"], sessions_dir=base)

    assert result == {"a\\b/c.py": ["sess-a"]}


def test_lookup_dot_dot_normalizing_input_resolves_claimed(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "a/b/c.py")])

    result = claim_index.lookup(["a/x/../b/./c.py"], sessions_dir=base)

    assert result == {"a/x/../b/./c.py": ["sess-a"]}


# ---------------------------------------------------------------------------
# C1d — edit_ts widening (state/audits/2026-08-13-edit-recency-spike.md)
# ---------------------------------------------------------------------------


def test_lookup_edit_ts_carries_last_t_timestamp_for_claimant(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base, "sess-a", [_touch_line("T", "foo.py", when="2026-08-13T10:00:00.000000Z")]
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    from datetime import datetime, timezone

    assert result.edit_ts["foo.py"] == {
        "sess-a": datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
    }


def test_lookup_edit_ts_absent_for_unclaimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    result = claim_index.lookup(["never/touched.py"], sessions_dir=base)

    assert result.edit_ts.get("never/touched.py") is None


def test_lookup_edit_ts_removed_on_release(tmp_path):
    base = str(tmp_path)
    _session_touched(
        base,
        "sess-a",
        [
            _touch_line("T", "foo.py", when="2026-08-13T10:00:00.000000Z"),
            _touch_line("R", "foo.py", when="2026-08-13T10:00:05.000000Z"),
        ],
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result["foo.py"] == []
    assert result.edit_ts.get("foo.py") is None


# `test_lookup_edit_ts_ignores_unparseable_timestamp` removed by C7b: it
# pinned a bare-path legacy `touched.txt` line (no verb, no timestamp) being
# skipped by this module's OLD reader. That scenario has no counterpart in
# the new `touch-record.jsonl` dialect this module now reads exclusively --
# `touch_record.encode_line`/`decode_line` require every field, so a "bare
# path, unknown time" event cannot be constructed or fed through the seam at
# all. See this chunk's own report for the C7b AC21-deletion writeup.


# ---------------------------------------------------------------------------
# C2 — recorded_name widening (docs/plans/2026-09-01-the-claim-record-
# carries-the-name.md). Mirrors the edit_ts block above byte-for-byte on
# shape and lifecycle: populated on TOUCH, popped on RELEASE, absent (never
# a degrade signal) for a claimant whose event carries no name.
# ---------------------------------------------------------------------------


def test_lookup_value_shape_is_byte_compatible_plain_dict_of_lists(tmp_path):
    """The `.recorded_name` widening is ADDITIVE-ATTRIBUTE-ONLY -- it must
    never reshape `lookup()`'s own `{path: [sid, ...]}` value list into
    tuples or any other shape. `session.claims` (`claim_index.lookup([path],
    ...).get(path, [])`) and `ops.session.safe_commit_offer` both consume
    those list values directly; this pins the literal shape both depend on."""
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result == {"foo.py": ["sess-a"]}
    assert type(result["foo.py"]) is list
    assert result["foo.py"] == ["sess-a"]
    assert all(isinstance(sid, str) for sid in result["foo.py"])


def test_lookup_recorded_name_carries_name_for_claimant(tmp_path):
    base = str(tmp_path)
    _session_touched_named(
        base, "sess-a", [(_touch_line("T", "foo.py"), "claude-klabauter-57")]
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result.recorded_name["foo.py"] == {"sess-a": "claude-klabauter-57"}


def test_lookup_recorded_name_absent_when_event_carries_no_name(tmp_path):
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result["foo.py"] == ["sess-a"]
    assert result.recorded_name.get("foo.py") is None


def test_lookup_recorded_name_absent_for_unclaimed_path(tmp_path):
    base = str(tmp_path)
    _session_touched_named(
        base, "sess-a", [(_touch_line("T", "foo.py"), "claude-klabauter-57")]
    )

    result = claim_index.lookup(["never/touched.py"], sessions_dir=base)

    assert result.recorded_name.get("never/touched.py") is None


def test_lookup_recorded_name_removed_on_release(tmp_path):
    base = str(tmp_path)
    _session_touched_named(
        base,
        "sess-a",
        [
            (_touch_line("T", "foo.py", when="2026-08-13T10:00:00.000000Z"), "claude-klabauter-57"),
            (_touch_line("R", "foo.py", when="2026-08-13T10:00:05.000000Z"), None),
        ],
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result["foo.py"] == []
    assert result.recorded_name.get("foo.py") is None


def test_lookup_recorded_name_survives_reclaim_after_release_by_new_name(tmp_path):
    """Last-event-wins applies to the recorded name too: a re-claim after a
    release carries whatever name (if any) the RE-claim's own event
    recorded, never the earlier claim's stale name."""
    base = str(tmp_path)
    _session_touched_named(
        base,
        "sess-a",
        [
            (_touch_line("T", "foo.py", when="2026-08-13T10:00:00.000000Z"), "claude-klabauter-57"),
            (_touch_line("R", "foo.py", when="2026-08-13T10:00:05.000000Z"), None),
            (_touch_line("T", "foo.py", when="2026-08-13T10:00:10.000000Z"), "claude-klabauter-99"),
        ],
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result["foo.py"] == ["sess-a"]
    assert result.recorded_name["foo.py"] == {"sess-a": "claude-klabauter-99"}


def test_lookup_recorded_name_no_extra_io_no_git_spawn(tmp_path, monkeypatch):
    """Same cost-class negative spec as ``edit_ts``: the name was already
    parsed off every touch-record line before this widening, and no new
    file read or subprocess spawn is introduced by carrying it through."""
    import subprocess

    def _forbid_spawn(*args, **kwargs):
        raise AssertionError("claim_index must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbid_spawn)
    monkeypatch.setattr(subprocess, "Popen", _forbid_spawn)

    base = str(tmp_path)
    _session_touched_named(
        base, "sess-a", [(_touch_line("T", "foo.py"), "claude-klabauter-57")]
    )

    result = claim_index.lookup(["foo.py"], sessions_dir=base)

    assert result.recorded_name["foo.py"] == {"sess-a": "claude-klabauter-57"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# commit_set — "what belongs to me to commit?"
# ---------------------------------------------------------------------------


def test_commit_set_returns_this_sessions_outstanding_paths(tmp_path):
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "a.py"), _touch_line("T", "b.py")])
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "c.py")])

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.paths == ["a.py", "b.py"]
    assert result.complete is True
    assert result.contested == {}


def test_commit_set_excludes_a_released_path(tmp_path):
    # Committing releases the claim, so the answer is "still outstanding",
    # never "ever touched" -- the distinction the PM named explicitly.
    _session_touched(
        tmp_path,
        "sid-mine",
        [
            _touch_line("T", "kept.py"),
            _touch_line("T", "committed.py"),
            _touch_line("R", "committed.py", when="2026-08-08T11:00:00.000000Z"),
        ],
    )

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.paths == ["kept.py"]


def test_commit_set_withholds_a_contested_path_but_names_it(tmp_path):
    # A peer's path is not yours, so it is not offered -- but it IS named, so
    # the operator learns why something they edited is missing instead of
    # wondering. Silent omission would reintroduce the doubt this removes.
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "shared.py"), _touch_line("T", "solo.py")])
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "shared.py")])

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.paths == ["solo.py"]
    assert result.contested == {"shared.py": ["sid-peer"]}


def test_commit_set_includes_paths_an_agent_touched_for_this_session(tmp_path):
    """INVERTED (C3, docs/plans/2026-08-27-safe-commit-offer-excludes-a-
    live-agent.md): this test used to assert that a path claimed SOLELY
    through a dispatched agent's own touch record joined ``paths`` alongside
    the session's own direct claim -- the exact over-reach this plan exists
    to correct (the same defect ``test_commit_set_offers_a_live_agents_
    inflight_claim_as_the_ems_own`` pins in isolation, below). C2 already
    split that attribution out of ``paths`` into ``in_flight_agent_claims``;
    this test now asserts THAT split for a claim set that mixes both
    sources, so a future reader does not "restore" the old union."""
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "em.py")])
    _agent_touched(tmp_path, "agent-1", "sid-mine", [_touch_line("T", "worker.py")])

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.paths == ["em.py"]
    assert result.in_flight_agent_claims == {"worker.py": ["agent-1"]}


def test_commit_set_spawns_no_subprocess(tmp_path, monkeypatch):
    # The whole point. The mechanism this replaces cost 73 processes per call.
    import subprocess

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("commit_set spawned a subprocess: %r" % (args,))

    monkeypatch.setattr(subprocess, "run", _explode)
    monkeypatch.setattr(subprocess, "Popen", _explode)
    monkeypatch.setattr(subprocess, "check_output", _explode)

    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "a.py")])

    assert claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path)).paths == ["a.py"]


def test_commit_set_propagates_an_unresolvable_base_as_incomplete(monkeypatch):
    # An aborted or unresolvable walk may under-report BOTH buckets, so the
    # caller must be able to say "this may be partial" rather than presenting
    # a short answer as THE answer. Note the contract this does NOT test: a
    # sessions_dir that simply does not exist resolves fine and honestly means
    # "no claims here" (complete=True, paths=[]) -- absence of a directory is
    # an answer, absence of a resolution is not.
    monkeypatch.setattr(claim_index, "_resolve_base", lambda *a, **k: "")

    result = claim_index.commit_set("sid-mine", sessions_dir="/irrelevant")

    assert result.complete is False
    assert result.abort_cause == claim_index.ABORT_CAUSE_EMPTY_BASE
    assert result.paths == []


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-agent.md) --
# reproduction: a live agent's in-flight claim is offered as the EM's own.
# ---------------------------------------------------------------------------


def test_commit_set_offers_a_live_agents_inflight_claim_as_the_ems_own(tmp_path):
    """INVERTED (C3, docs/plans/2026-08-27-safe-commit-offer-excludes-a-
    live-agent.md): observed shape, that plan's Problem section -- during
    docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md
    a dispatched executor was mid-write on a research artifact, still LIVE,
    when ``safe_commit_offer`` committed that same path as the EM's own --
    the EM session itself never touched it.

    THIS TEST USED TO PIN THAT OVER-REACH ON PURPOSE (the RED-FIRST
    reproduction C2/C3 fixed against). C2 corrected it AT THIS SURFACE:
    ``commit_set`` performs no liveness check of its own (module docstring)
    -- it now reports a path claimed SOLELY through a dispatched agent's own
    touch record via ``in_flight_agent_claims``, attribution without a
    verdict, and keeps it OUT of ``paths`` unconditionally regardless of
    whether that agent is live or dead. The liveness VERDICT (live agent ->
    stays excluded; dead/undetermined agent -> folds back into what the
    session may commit) is resolved one layer up, at
    ``safe_commit_offer.compute_offer`` -- see
    ``TestComputeOffer::test_a_dispatched_agents_inflight_claim_is_offered_
    as_the_ems_own`` (now the corrected assertion) in this repo's
    ``coordinator_core/ops/session/tests/test_safe_commit_offer.py``."""
    base = str(tmp_path)
    _agent_touched(
        base, "agent-live", "sess-em", [_touch_line("T", "docs/research/in-flight.md")]
    )

    result = claim_index.commit_set("sess-em", sessions_dir=base)

    assert result.paths == []
    assert result.in_flight_agent_claims == {"docs/research/in-flight.md": ["agent-live"]}
    assert result.contested == {}
    assert result.peers == {}


def test_commit_set_reports_attribution_only_no_liveness_verdict_for_an_agent_claim(
    tmp_path,
):
    """MOVED here from ``test_commit_set_leaves_a_dead_agents_orphaned_claim_
    in_mine`` (C3, docs/plans/2026-08-27-safe-commit-offer-excludes-a-live-
    agent.md): that test asserted DEAD-agent behaviour at this surface, but
    ``commit_set`` performs NO liveness check of its own (module docstring)
    -- a guard asserting a dead-agent verdict here asserts something this
    surface is designed never to know, whether the agent behind the claim is
    live, dead, or undetermined. The real dead-agent regression guard now
    lives at ``safe_commit_offer.compute_offer``, the surface where
    liveness is actually resolved -- see ``TestComputeOffer::
    test_a_dead_agents_orphaned_claim_stays_in_mine`` in
    ``coordinator_core/ops/session/tests/test_safe_commit_offer.py``.

    What THIS surface knows, and all this test asserts: a path claimed
    SOLELY through a dispatched agent's own touch record is attributed to
    that agent and withheld from ``paths`` -- present instead in
    ``in_flight_agent_claims`` -- with no verdict rendered either way.
    """
    base = str(tmp_path)
    _agent_touched(
        base, "agent-1", "sess-em", [_touch_line("T", "docs/research/orphaned.md")]
    )

    result = claim_index.commit_set("sess-em", sessions_dir=base)

    assert result.paths == []
    assert result.in_flight_agent_claims == {"docs/research/orphaned.md": ["agent-1"]}
    assert result.contested == {}
    assert result.peers == {}


# ---------------------------------------------------------------------------
# classify_paths — "is THIS path mine?" for a pathspec already in hand
# ---------------------------------------------------------------------------


def test_classify_paths_separates_mine_from_peer_from_unclaimed(tmp_path):
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "mine.py")])
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "theirs.py")])

    answer = claim_index.classify_paths(
        "sid-mine", ["mine.py", "theirs.py", "nobody.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["mine.py"].verdict == claim_index.OWNERSHIP_MINE
    assert answer.by_path["theirs.py"].verdict == claim_index.OWNERSHIP_PEER
    assert answer.by_path["theirs.py"].peers == ["sid-peer"]
    assert answer.by_path["nobody.py"].verdict == claim_index.OWNERSHIP_UNCLAIMED
    assert answer.complete is True


def test_classify_paths_denies_a_path_a_peer_also_holds(tmp_path):
    # Shared with a peer is NOT mine: `commit_set` withholds it, and the gate
    # must refuse it, or the two answers disagree about the same path.
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "shared.py")])
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "shared.py")])

    answer = claim_index.classify_paths(
        "sid-mine", ["shared.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["shared.py"].verdict == claim_index.OWNERSHIP_PEER
    assert answer.by_path["shared.py"].peers == ["sid-peer"]


def test_classify_paths_never_answers_mine_or_unclaimed_on_an_aborted_walk(
    tmp_path, monkeypatch
):
    # C10's fail-open, closed by construction: both of those verdicts are
    # claims about what the walk did NOT find, and a walk that stopped early
    # found nothing it did not reach.
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "mine.py")])
    real_rebuild = claim_index.rebuild

    def _incomplete(*args, **kwargs):
        state = real_rebuild(*args, **kwargs)
        state.complete = False
        state.abort_cause = claim_index.ABORT_CAUSE_CAP_EXCEEDED
        return state

    monkeypatch.setattr(claim_index, "rebuild", _incomplete)

    answer = claim_index.classify_paths(
        "sid-mine", ["mine.py", "nobody.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["mine.py"].verdict == claim_index.OWNERSHIP_UNANSWERABLE
    assert answer.by_path["nobody.py"].verdict == claim_index.OWNERSHIP_UNANSWERABLE
    assert answer.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED


def test_classify_paths_still_names_a_peer_found_before_the_abort(tmp_path, monkeypatch):
    # A peer claim this walk DID reach is a fact the abort does not undo.
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "theirs.py")])
    real_rebuild = claim_index.rebuild

    def _incomplete(*args, **kwargs):
        state = real_rebuild(*args, **kwargs)
        state.complete = False
        state.abort_cause = claim_index.ABORT_CAUSE_IO_ERROR
        return state

    monkeypatch.setattr(claim_index, "rebuild", _incomplete)

    answer = claim_index.classify_paths(
        "sid-mine", ["theirs.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["theirs.py"].verdict == claim_index.OWNERSHIP_PEER


def test_classify_paths_credits_this_sessions_agent(tmp_path):
    _agent_touched(tmp_path, "agent-1", "sid-mine", [_touch_line("T", "worker.py")])

    answer = claim_index.classify_paths(
        "sid-mine", ["worker.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["worker.py"].verdict == claim_index.OWNERSHIP_MINE


def test_classify_paths_normalizes_the_callers_path_dialect(tmp_path):
    # A backslashed pathspec and the forward-slash key touched.txt records are
    # the same path; the answer is keyed by what the CALLER passed.
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "pkg/mod.py")])

    answer = claim_index.classify_paths(
        "sid-mine", [r"pkg\mod.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path[r"pkg\mod.py"].verdict == claim_index.OWNERSHIP_MINE


def test_classify_paths_spawns_no_subprocess(tmp_path, monkeypatch):
    # This runs on the COMMIT HOT PATH -- every dispatched-committer
    # invocation pays it. The mechanism it replaces cost 73 processes.
    import subprocess

    def _explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("classify_paths spawned a subprocess: %r" % (args,))

    monkeypatch.setattr(subprocess, "run", _explode)
    monkeypatch.setattr(subprocess, "Popen", _explode)
    monkeypatch.setattr(subprocess, "check_output", _explode)

    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "a.py")])

    answer = claim_index.classify_paths(
        "sid-mine", ["a.py"], sessions_dir=str(tmp_path)
    )

    assert answer.by_path["a.py"].verdict == claim_index.OWNERSHIP_MINE


def test_commit_set_names_peer_only_claims_separately_from_contested(tmp_path):
    # `paths`/`contested` answer "what is mine"; `peers` answers the different
    # question a dirty-tree sweep asks about a path it did NOT get from here --
    # "does a peer own this, or has nobody claimed it?". Collapsing the two
    # reads a peer's in-flight file as unattributed.
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "mine.py"), _touch_line("T", "shared.py")])
    _session_touched(tmp_path, "sid-peer", [_touch_line("T", "shared.py"), _touch_line("T", "theirs.py")])

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.paths == ["mine.py"]
    assert result.contested == {"shared.py": ["sid-peer"]}
    assert result.peers == {"theirs.py": ["sid-peer"]}


def test_commit_set_peers_excludes_a_path_nobody_holds(tmp_path):
    _session_touched(tmp_path, "sid-mine", [_touch_line("T", "mine.py")])

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.peers == {}


def test_commit_set_peers_drops_a_released_peer_claim(tmp_path):
    # Last-event-wins applies here exactly as it does to `paths`: a peer that
    # committed and released no longer holds the path, so it is nobody's.
    _session_touched(
        tmp_path,
        "sid-peer",
        [
            _touch_line("T", "theirs.py"),
            _touch_line("R", "theirs.py", when="2026-08-08T11:00:00.000000Z"),
        ],
    )

    result = claim_index.commit_set("sid-mine", sessions_dir=str(tmp_path))

    assert result.peers == {}


# `test_unmigrated_writer_claim_is_still_seen_during_the_cutover` and
# `test_old_format_record_is_not_a_malformed_new_format_record` removed by
# C7b: both pinned the AC21 transitional union-read (touch-record.jsonl
# UNION legacy touched.txt) that this chunk deletes by name and in full --
# `_read_stream_claims`/`_enumerate_claim_sinks` no longer look at
# `touched.txt` at all, so "an unmigrated claimant's legacy file is still
# seen" and "old bytes don't read as corrupt" are no longer properties this
# module has (or needs -- C7c deletes the legacy WRITER too). See this
# chunk's own report for the AC21-deletion writeup; no replacement pin is
# owed, mirroring C5's removal of the peer-release tests in
# test_scope.py (scope.py module docstring, `release_committed_claims`).


# ---------------------------------------------------------------------------
# C8 / AC18 — the process-time re-key is pinned, and the abort residual is
# re-measured at Corpus C width post-cutover.
# docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md
# ---------------------------------------------------------------------------


def test_ac18_rebuild_cap_is_immune_to_time_monotonic(tmp_path, monkeypatch):
    """AC18's second half, pin (not redo): confirm
    ``REBUILD_PROCESS_TIME_CAP_SECS`` is read against ``time.process_time()``,
    never ``time.monotonic()``. Freezes ``time.monotonic`` at a value that
    would trip an old-style wall-clock deadline instantly (module-wide, for
    the whole rebuild) while leaving the real ``time.process_time()`` free
    to advance normally -- a rebuild that still completes proves nothing in
    this module reads the frozen clock at all, closing the exact gap the
    module docstring names (a wall-clock cap "measures PEER LOAD, not this
    index's cost" at 50-70 concurrent sessions)."""
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    _session_touched(base, "sess-b", [_touch_line("T", "bar.py")])

    monkeypatch.setattr(claim_index.time, "monotonic", lambda: 1e12)

    state = claim_index.rebuild(sessions_dir=base)

    assert state.complete is True
    assert state.abort_cause is None
    assert state.claims == {"foo.py": ["sess-a"], "bar.py": ["sess-b"]}


def test_ac18_lookup_cap_withheld_returns_unanswerable_never_empty(tmp_path, monkeypatch):
    """AC18's second half, other direction: a cap that withholds returns
    C3's typed ``UNANSWERABLE`` signal, never a silent empty set. Already
    exercised via ``REBUILD_PROCESS_TIME_CAP_SECS=-1.0`` above
    (``test_lookup_cap_exceeded_resolves_unanswerable_not_unclaimed``); this
    is the same property driven through the process-time construction
    instead, so the pin does not depend on the wall-clock-shaped patch
    surviving a future edit to that constant's own semantics."""
    base = str(tmp_path)
    _session_touched(base, "sess-a", [_touch_line("T", "foo.py")])
    _session_touched(base, "sess-b", [_touch_line("T", "bar.py")])

    ticks = iter([0.0, 0.0, 10_000.0] + [10_000.0] * 64)
    monkeypatch.setattr(claim_index.time, "process_time", lambda: next(ticks))

    result = claim_index.lookup(["foo.py", "bar.py"], sessions_dir=base)

    assert result.complete is False
    assert result.abort_cause == claim_index.ABORT_CAUSE_CAP_EXCEEDED
    # sess-a sorts first and is consumed before the deadline trips; sess-b
    # is never reached -- its resolved-known claim (`foo.py`) is NOT
    # downgraded to UNANSWERABLE by the abort (a peer claim the walk DID
    # reach is a fact the abort does not undo), while the path the walk
    # never reached comes back UNANSWERABLE, never a silent `[]`.
    assert result["foo.py"] == ["sess-a"]
    assert result["bar.py"] == [claim_index.UNANSWERABLE]


def _projected_depth(rank: int, claimant_count: int) -> int:
    """Events for the claimant at sorted position *rank* of
    *claimant_count*, reproducing the depth distribution MEASURED off the
    live corpus rather than a chosen number.

    See `_MEASURED_DEPTH_DECILES` for the measurement. The tail is
    reproduced explicitly because it is where a per-claimant read cost
    concentrates: p95, p99 and the single deepest claimant each get their
    measured value instead of being flattened into the top decile.
    """
    quantile = rank / claimant_count
    if quantile >= 1.0 - (1.0 / claimant_count):
        return _MEASURED_DEPTH_MAX
    if quantile >= 0.99:
        return _MEASURED_DEPTH_P99
    if quantile >= 0.95:
        return _MEASURED_DEPTH_P95
    return _MEASURED_DEPTH_DECILES[int(quantile * 10)]


def _write_projected_claimant(base: Path, index: int, depth: int) -> None:
    """One claim-bearing directory holding *depth* T/R events on one sink,
    written as raw encoded bytes. Verb churns 80% T / 20% R.

    `depth` 0 writes an EMPTY sink, not a missing one -- the measured
    bottom decile is a claimant whose file exists and holds nothing, which
    `_has_claim_surface` answers on the cheap `isfile` arm. That is a
    different cost from a directory with no sink at all
    (`_write_projected_empty_dir`), and the two must not be collapsed.
    """
    sid = f"projected-claimant-{index:05d}"
    sink = base / sid / "touch-record.jsonl"
    sink.parent.mkdir(parents=True, exist_ok=True)
    ts = 1_700_000_000.0
    lines = []
    for i in range(depth):
        verb = touch_record.VERB_RELEASE if i % 5 == 0 else touch_record.VERB_TOUCH
        lines.append(
            touch_record.encode_line(
                session_id=sid,
                agent_id=None,
                verb=verb,
                path=f"file_{i % 1000}.py",
                timestamp=ts + i * 0.01,
            )
        )
    sink.write_bytes(b"".join(lines))


def _write_projected_empty_dir(base: Path, index: int) -> None:
    """A session directory carrying NO touch record at all.

    WHY THIS DIRECTORY IS THE POINT, NOT PADDING. 221 of the live corpus's
    491 candidate directories are this shape, and each one is the EXPENSIVE
    case: `_has_claim_surface` short-circuits on `os.path.isfile` for a
    claimant that HAS a live sink, so only a sink-less directory falls
    through to `discover_family`, which `scandir`s it to look for rotated
    generations that (measured: zero anywhere on the box) are not there.
    That fall-through is 20.27ms of the live walk's ~33ms enumerate cost.

    Corpus C modelled 50 directories that ALL had live sinks -- zero
    fall-throughs -- which is why it reported a 0.39ms enumerate floor for
    a corpus whose real floor is two orders of magnitude higher.
    """
    (base / f"projected-empty-{index:05d}").mkdir(parents=True, exist_ok=True)


def _write_rebuild_driver(
    driver_path: Path, sessions_dir: Path, cap_secs: Optional[float] = None
) -> None:
    """Driver that times one `rebuild()` over `sessions_dir`.

    `cap_secs` lifts `REBUILD_PROCESS_TIME_CAP_SECS` out of the way so the
    WORK can be priced.

    WHY THAT IS NOT CHEATING, AND WHY THE DEFAULT WOULD BE. `rebuild()`
    self-aborts the moment its own cap is exceeded and returns
    `complete=False, abort_cause='cap_exceeded'`. Timing the capped call
    therefore measures the CAP, not the walk: at Corpus C it returns
    ~500ms whatever the corpus costs, because ~500ms is when it gives up
    (measured: 531ms capped, having reached 16 of 50 peers, vs 766ms for
    the complete walk). A number pinned to its own governor cannot answer
    "is this site under the bar" -- it can only ever say "the cap works".
    That is how a 541.48ms figure survived this whole workstream looking
    like a near-miss when the real cost is half again the brightline.
    """
    cap_line = (
        f"claim_index.REBUILD_PROCESS_TIME_CAP_SECS = {cap_secs!r}\n"
        if cap_secs is not None
        else ""
    )
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from coordinator_core.session import claim_index
        """
    ) + cap_line + textwrap.dedent(
        f"""\
        state = claim_index.rebuild(sessions_dir={str(sessions_dir)!r})
        print(state.complete, len(state.claims), state.abort_cause)
        """
    )
    driver_path.write_text(script, encoding="utf-8")


def _write_rebuild_floor_driver(driver_path: Path) -> None:
    """The AC18 driver with `rebuild()` removed and nothing else changed.

    NEGATIVE SPEC -- this must stay byte-identical to `_write_rebuild_driver`
    above its `rebuild()` call, imports included. Its whole job is to price
    the interpreter start and the `claim_index` import graph so they can be
    subtracted out; a floor that imports less than the real driver
    under-states the floor and over-states `rebuild()`, which is the same
    apples-to-oranges error in the opposite direction.
    """
    script = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
        from coordinator_core.session import claim_index
        """
    )
    driver_path.write_text(script, encoding="utf-8")


#: THE LIVE CORPUS, MEASURED 2026-08-27 -- not a chosen width.
#: `.git/coordinator-sessions/` on this box, read through
#: `_enumerate_claim_sinks` itself (session dirs plus `.agents/`), after
#: 43.7 days of accumulation with NO retention prune of claimant dirs.
#: Re-measure with the probe recorded in
#: docs/research/spike-verdicts/2026-08-27-corpus-c-is-wrong-on-both-axes-
#: and-the-fingerprint-prize-collapses-at-real-width.md before changing any
#: figure below; none of them is an estimate.
_MEASURED_CANDIDATE_DIRS = 491
_MEASURED_CLAIMANTS = 270
_MEASURED_EVENTS = 2561
_MEASURED_WINDOW_DAYS = 43.7
#: Per-claimant event depth, deciles 0..9 of the measured distribution.
#: mean 9.49, median 5. The p95/p99/max tail is carried separately because
#: flattening it into the top decile understates exactly the claimants a
#: per-claimant read cost concentrates on.
_MEASURED_DEPTH_DECILES = (0, 1, 2, 3, 4, 5, 7, 10, 12, 16)
_MEASURED_DEPTH_P95 = 33
_MEASURED_DEPTH_P99 = 70
_MEASURED_DEPTH_MAX = 169

#: THE PROJECTION, and the one judgement call in this fixture: one year of
#: the SAME measured accumulation rate. Claimant directories are never
#: pruned (no retention mechanism exists -- see the problem doc's Item 0),
#: so the corpus grows monotonically and the only free variable is the
#: horizon. One year is stated, not derived; every other number here is
#: measured and scales from it.
_PROJECTION_HORIZON_DAYS = 365.0
_PROJECTION_FACTOR = _PROJECTION_HORIZON_DAYS / _MEASURED_WINDOW_DAYS
_PROJECTED_CLAIMANTS = round(_MEASURED_CLAIMANTS * _PROJECTION_FACTOR)
_PROJECTED_EMPTY_DIRS = (
    round(_MEASURED_CANDIDATE_DIRS * _PROJECTION_FACTOR) - _PROJECTED_CLAIMANTS
)

#: CORPUS C (50 peers x 5000 lines, and C0's 541.48ms against it) IS
#: RETIRED as a width, 2026-08-27, by measurement and NOT to make anything
#: green. Its constants are deleted rather than left unreferenced: it was
#: wrong on BOTH axes in OPPOSITE directions -- 5.2x UNDER on claimant
#: count (the axis the pre-parse floor scales with) and 35x OVER on
#: per-claimant depth -- and its 5000-line single sink is a shape no live
#: writer can emit, since `MAX_RECORD_BYTES` is 256KiB at a measured 197.5
#: bytes/event, so rotation fires near 1327 events per generation. Do not
#: reinstate it as a comparison baseline; a retired width is not a datum.
#:
#: `rebuild()` over the corpus that ACTUALLY exists today, cap lifted,
#: process time, two independent runs: 61.458ms both times -- 12.3% of the
#: brightline, `complete=True`, no cap abort. There is no LIVE breach at
#: this site. The gate below asserts against the PROJECTED width instead,
#: which is the honest question: does a year of unpruned accumulation
#: breach the bar.
_MEASURED_TODAY_MS = 61.458
_BRIGHTLINE_MS = 500.0


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac18_rebuild_at_projected_corpus_width_process_time_and_spawn_count(tmp_path):
    """AC18's gate: `claim_index.rebuild()` at ONE YEAR of the corpus growth
    this box actually exhibits, as process time and spawn count via
    `batched_process_time_ms`, never wall clock.

    NO LONGER `designed_red`, and NOT because anything was narrowed to make
    it green. The width was CHANGED on 2026-08-27 and the assertion was
    not; the change WIDENED the axis that costs, 45x on claimant count
    (50 -> 2255) and 8.4x on directory count, and the site passes anyway at
    **390.625ms** -- 109ms under the bar. AC18 is MET at every width that
    can be defended. See `_MEASURED_*` / `_PROJECTED_*` above for the
    measurement that retired the old one.

    RE-MEASURED 2026-09-01, and the crossing has arrived. In-process, over
    a fresh fixture of this exact width (2255 claimants x 5 events + 1846
    sink-less dirs), best-of-3 `time.process_time`: **515.6ms** -- one
    Windows timer tick over the 500ms bar, against the 390.625ms this
    docstring recorded on 2026-08-27. So this gate is now legitimately RED,
    and it is red for the reason the paragraph below predicted rather than
    for any regression in `rebuild()` itself: nothing prunes claimant
    directories, and the corpus kept growing. It was NOT caused by the
    `recorded_name` field added to `rebuild()` on 2026-09-01 (plan
    `2026-09-01-the-claim-record-carries-the-name`) -- that is per-claimant
    dict work, and the same measurement with and without it does not move
    515.6ms by the 125ms the gap would require.

    DO NOT NARROW THIS ASSERTION TO MAKE IT GREEN. The bar is the bar; what
    is owed is bounding the corpus, per the paragraph below.

    Beware the subprocess harness's own number here: through
    `batched_process_time_ms` the same walk reported 687.5ms and 928.1ms on
    two runs the same hour, against 515.6ms measured in-process. The
    subprocess arm builds the fixture and pays disk cost on a box carrying
    dozens of concurrent sessions, and the floor subtraction does not remove
    it. Price the walk in-process before believing a number from this gate.

    THE FINDING THAT SURVIVES, and it is not this assertion. Two points on
    the growth curve -- 61.458ms at the corpus that exists (43.7d) and
    390.625ms at one year of the same accumulation -- put the brightline
    crossing at roughly **472 days, about 15.5 months**, and NOTHING PRUNES
    CLAIMANT DIRECTORIES. This gate going green does not retire the item;
    it re-dates it. What answers it is bounding the corpus (shape (c) in
    the problem doc), not making a large one cheaper to re-read.

    WHY THE WIDTH CHANGED. This gate used to assert at Corpus C -- 50 peers
    x 5000 lines -- inherited unexamined from C0 and never once validated
    against disk. Measured through this module's own enumerator, that width
    is wrong on BOTH axes in OPPOSITE directions: the live corpus carries
    **270 claimants, not 50** (5.2x MORE), each holding a median of **5**
    events and at most **169**, not 5000 (35x FEWER). `rebuild()`'s
    pre-parse floor scales with CLAIMANT COUNT, so the fixture was
    understating the axis that costs while overstating the one that does
    not.

    WHAT THAT COST. Corpus C's 50 directories all carry live sinks, so
    `_has_claim_surface` short-circuits on `isfile` for every one of them
    and the fixture reports a 0.39ms enumerate floor. The live corpus has
    **221 of 491 candidate directories with no sink at all**, and each
    falls through to `discover_family`, which `scandir`s it to find rotated
    generations that do not exist. Real enumerate floor: **21.5ms**, of
    which 20.27ms is that fall-through and 12.71ms is `em-session-id.txt`
    back-pointer opens, against a bare walk of 0.41ms. Two orders of
    magnitude, entirely invisible to the old fixture. `_write_projected_
    empty_dir` exists to stop that recurring.

    THERE IS NO LIVE BREACH AT THIS SITE. Cap lifted, over the corpus that
    exists today, twice: **61.458ms** -- 12.3% of the brightline,
    `complete=True`, no cap abort. The 541 / 537ms figures this workstream
    ran on were a capped call timing its own cap (see
    `_write_rebuild_driver`) at a width that has never existed here.

    WHAT THIS GATE THEREFORE ASKS: nothing prunes claimant directories --
    no retention mechanism exists -- so the corpus grows monotonically and
    the honest question is whether a year of the SAME accumulation breaches
    the bar. Measured answer: no, at 78% of it.

    Spawn count is 1.0 -- pure in-process CPU, so no batching or
    spawn-cutting fix reaches it. But the profile that follows from the
    corrected width is NOT the parse-bound one Corpus C showed (~72% in
    `decode_line`): at real proportions roughly half the walk is
    per-claimant syscalls before a byte is decoded. "Read less, do not
    parse faster" survives; WHAT to read less of has changed, and shape (c)
    -- bound the corpus -- is the shape that answers this gate, not the
    fingerprinted cache, whose own floor is 57% of the walk it replaces.

    NEGATIVE SPEC. Do not narrow the corpus and do not re-cap this driver
    to make this green. The width above moved on measurement, with the
    measurement recorded; that is the ONLY licence there is for touching
    it."""
    base = tmp_path / "projected-corpus"
    base.mkdir(parents=True, exist_ok=True)
    for rank in range(_PROJECTED_CLAIMANTS):
        _write_projected_claimant(
            base, rank, _projected_depth(rank, _PROJECTED_CLAIMANTS)
        )
    for index in range(_PROJECTED_EMPTY_DIRS):
        _write_projected_empty_dir(base, index)

    # Cap lifted: price the WALK, not the governor. See
    # `_write_rebuild_driver`'s docstring for why the capped call cannot
    # answer this AC's question.
    driver = tmp_path / "rebuild_driver.py"
    _write_rebuild_driver(driver, base, cap_secs=600.0)

    # The startup floor is measured, never assumed. `batched_process_time_ms`
    # times a SPAWNED process, so the full driver's figure carries this
    # interpreter start plus the `claim_index` import graph on top of
    # `rebuild()`'s own cost. Every figure this gate compares against is a
    # `rebuild()`-only one, so comparing the startup-inclusive total
    # against them is apples-to-oranges and manufactures a regression that
    # is not there. Subtract, then
    # compare like for like -- and gate on the subtracted number, because
    # the interpreter floor is not this index's cost to answer for.
    floor_driver = tmp_path / "rebuild_floor_driver.py"
    _write_rebuild_floor_driver(floor_driver)

    floor = batched_process_time_ms([sys.executable, str(floor_driver)], k=5)
    result = batched_process_time_ms([sys.executable, str(driver)], k=5)

    assert floor["rc"] == 0, f"floor driver failed: {floor!r}"
    assert result["rc"] == 0, f"rebuild driver failed: {result!r}"

    rebuild_only_ms = round(result["process_time_ms"] - floor["process_time_ms"], 3)
    delta_vs_today = round(rebuild_only_ms - _MEASURED_TODAY_MS, 3)
    delta_vs_bar = round(rebuild_only_ms - _BRIGHTLINE_MS, 3)
    verdict = "PASSES" if rebuild_only_ms <= _BRIGHTLINE_MS else "FAILS"
    detail = (
        f"AC18 projected-width rebuild(): rebuild_only="
        f"{rebuild_only_ms}ms (total {result['process_time_ms']}ms minus "
        f"{floor['process_time_ms']}ms interpreter+import floor) "
        f"procs_per_call={result['procs_per_call']} (floor "
        f"{floor['procs_per_call']}, excess "
        f"{round(result['procs_per_call'] - floor['procs_per_call'], 3)}) (k={result['k']}) at "
        f"{_PROJECTED_CLAIMANTS} claimants + {_PROJECTED_EMPTY_DIRS} "
        f"sink-less dirs -- {_PROJECTION_HORIZON_DAYS:.0f}d of the growth "
        f"measured over {_MEASURED_WINDOW_DAYS}d "
        f"({_MEASURED_CLAIMANTS} claimants, {_MEASURED_EVENTS} events, "
        f"{_MEASURED_TODAY_MS}ms today, delta {delta_vs_today}ms) vs the "
        f"{_BRIGHTLINE_MS}ms brightline (delta {delta_vs_bar}ms) -- "
        f"AC18 {verdict}."
    )
    print(detail)
    # DIFFERENTIAL, not absolute -- for the same reason `rebuild_only_ms`
    # subtracts the floor rather than asserting against the total. Measured
    # 2026-09-01: a driver whose entire body is `print('x')`, importing no
    # coordinator module and calling no `rebuild()`, also reports
    # `procs_per_call=2.0` through this harness on Windows. The absolute
    # `== 1.0` form therefore priced the HARNESS, not the code under test,
    # and could not pass on this box whatever `rebuild()` did -- a live gate
    # red for a reason no change to `claim_index` could ever clear.
    #
    # What the assertion is actually for survives intact and is what is
    # asserted here: `rebuild()` must spawn NO subprocess BEYOND what
    # importing its own module graph already costs. The floor driver is
    # byte-identical to the real one above its `rebuild()` call (see
    # `_write_rebuild_floor_driver`'s negative spec), so any excess is
    # `rebuild()`'s and nothing else's. Independently corroborated the same
    # day by instrumenting `subprocess.run` across a live `rebuild()` over a
    # synthetic 30-session / 600-event corpus: zero calls.
    procs_excess = result["procs_per_call"] - floor["procs_per_call"]
    assert procs_excess == pytest.approx(0.0, abs=0.01), (
        f"a pure-Python rebuild driver must spawn no subprocess of its own "
        f"BEYOND its import floor: driver={result['procs_per_call']} "
        f"floor={floor['procs_per_call']} excess={procs_excess}. {detail}"
    )
    # The real AC18 gate, and it holds at one year of measured growth. It
    # is a LIVE gate now, not `designed_red`: a regression here means the
    # walk got more expensive, not that a known-open defect is still open.
    assert rebuild_only_ms <= _BRIGHTLINE_MS, (
        f"AC18 UNMET: {detail}"
    )
