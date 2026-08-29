"""
coordinator_core.session.tests.test_touch_record — coverage for the
touch-record line format and its append path.

Spec backlink:
docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md (C2)
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

import coordinator_core.session.touch_record as touch_record
from coordinator_core.session.touch_record import (
    MAX_ENCODED_LINE_LEN,
    MAX_RECORD_BYTES,
    VERB_RELEASE,
    VERB_TOUCH,
    LineTooLong,
    MalformedRecordLine,
    OutOfWorktreePath,
    TouchEvent,
    TouchProjection,
    append_event,
    compact_record,
    decode_line,
    degrade_counts,
    discover_family,
    encode_line,
    project_live_claims,
)


def test_round_trip_encode_decode():
    encoded = encode_line(
        session_id="sid-1", agent_id="scout@session-abcd", verb="T",
        path="coordinator_core\\session\\scope.py", timestamp=1234.5,
    )
    assert encoded.endswith(b"\n")
    event = decode_line(encoded)
    assert event == TouchEvent(
        schema_version=1,
        verb="T",
        timestamp=1234.5,
        session_id="sid-1",
        agent_id="scout@session-abcd",
        path="coordinator_core/session/scope.py",
    )


def test_agent_keyed_line_attributes_without_directory_context():
    encoded = encode_line(
        session_id="sid-1", agent_id="reviewer@session-9f2a", verb="T", path="a/b.py",
    )
    event = decode_line(encoded)
    assert event.agent_id == "reviewer@session-9f2a"
    assert event.session_id == "sid-1"


def test_session_keyed_line_carries_explicit_null_agent():
    encoded = encode_line(session_id="sid-1", agent_id=None, verb="R", path="a/b.py")
    event = decode_line(encoded)
    assert event.agent_id is None


def test_malformed_line_is_rejected_with_typed_signal():
    with pytest.raises(MalformedRecordLine):
        decode_line(b"not json at all\n")

    with pytest.raises(MalformedRecordLine):
        decode_line(json.dumps({"v": 1, "verb": "X", "ts": 1.0, "sid": "s", "agent": None, "path": "a"}) + "\n")

    with pytest.raises(MalformedRecordLine):
        decode_line(json.dumps({"v": 1, "verb": "T", "ts": 1.0}) + "\n")


def test_line_at_or_over_length_bound_rejected_at_encode_time(tmp_path):
    long_path = "x" * MAX_ENCODED_LINE_LEN
    with pytest.raises(LineTooLong):
        encode_line(session_id="sid-1", agent_id=None, verb="T", path=long_path)

    sink = tmp_path / "touch-record.jsonl"
    with pytest.raises(LineTooLong):
        append_event(sink, session_id="sid-1", agent_id=None, verb="T", path=long_path)
    assert not sink.exists()


def test_every_encoded_line_ends_with_exactly_one_trailing_newline():
    encoded = encode_line(session_id="sid-1", agent_id=None, verb="T", path="a/b.py")
    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")
    assert encoded.count(b"\n") == 1


def test_append_event_creates_parent_dir_and_routes_through_atomic_append(tmp_path):
    sink = tmp_path / "nested" / "dir" / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a/b.py")
    assert sink.parent.is_dir()
    lines = sink.read_bytes().splitlines(keepends=True)
    assert len(lines) == 1
    event = decode_line(lines[0])
    assert event.path == "a/b.py"


def test_locked_rmw_not_on_the_append_path():
    """The append path never imports or calls locked_rmw -- only the module
    docstring's negative-spec prose is allowed to name it."""
    import coordinator_core.session.touch_record as mod

    assert not hasattr(mod, "locked_rmw")
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "import locked_rmw" not in source
    assert "locked_rmw(" not in source


def _child_append(sink_path_str: str, n: int, idx: int) -> None:
    """Multiprocessing worker mirroring
    coordinator_core.telemetry.tests.test_op_latency's own concurrency
    harness -- a fresh spawn-context process re-imports the module, so this
    exercises append_event's real atomic_append.append_line call, not a
    mock."""
    from coordinator_core.session.touch_record import append_event

    for i in range(n):
        append_event(
            sink_path_str,
            session_id=f"sid-{idx}",
            agent_id=None,
            verb="T",
            path=f"path/{idx}/{i}.py",
        )


def test_concurrent_append_from_n_processes_loses_no_line(tmp_path):
    sink = tmp_path / "touch-record.jsonl"

    n_procs = 4
    n_per_proc = 25
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_child_append, args=(str(sink), n_per_proc, idx))
        for idx in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    lines = sink.read_bytes().splitlines(keepends=True)
    assert len(lines) == n_procs * n_per_proc

    seen_by_idx = {idx: 0 for idx in range(n_procs)}
    for line in lines:
        event = decode_line(line)  # raises if interleave-corrupted
        assert event.session_id.startswith("sid-")
        idx = int(event.session_id.rsplit("-", 1)[1])
        seen_by_idx[idx] += 1

    assert seen_by_idx == {idx: n_per_proc for idx in range(n_procs)}


def test_ac17_growth_bound_is_invoked_live_by_append_event(tmp_path, monkeypatch):
    sink = tmp_path / "touch-record.jsonl"
    monkeypatch.setattr("coordinator_core.session.touch_record.MAX_RECORD_BYTES", 200)

    for i in range(50):
        append_event(sink, session_id="sid-1", agent_id=None, verb="T", path=f"a/{i}.py")

    size_after = sink.stat().st_size
    assert size_after < 200 * 50

    lines = sink.read_bytes().splitlines(keepends=True)
    paths = {decode_line(line).path for line in lines}
    assert "a/49.py" in paths


def _child_append_with_small_bound(sink_path_str: str, n: int, idx: int, bound: int) -> None:
    """Like ``_child_append``, but lowers ``MAX_RECORD_BYTES`` in this
    spawned process before appending, so the run crosses the growth-control
    bound multiple times while N processes are appending to the SAME sink
    concurrently -- the scenario
    ``test_concurrent_append_from_n_processes_loses_no_line`` never
    exercises (its default 256KiB bound is never crossed by 100 short
    lines)."""
    from coordinator_core.session import touch_record as mod

    mod.MAX_RECORD_BYTES = bound
    for i in range(n):
        mod.append_event(
            sink_path_str,
            session_id=f"sid-{idx}",
            agent_id=None,
            verb="T",
            path=f"path/{idx}/{i}.py",
        )


def test_concurrent_append_across_growth_control_loses_no_line(tmp_path):
    """Regression for the growth-control race (AC17): N processes appending
    concurrently to the same sink, with the bound low enough to be crossed
    repeatedly mid-run, must never lose, corrupt, or fail to record any
    process's own append. Fails against a whole-file-replace-in-place
    growth-control mechanism: os.replace racing a peer's open append
    either raises (Windows) mid-run, or silently orphans a peer's in-flight
    write (POSIX)."""
    sink = tmp_path / "touch-record.jsonl"

    n_procs = 4
    n_per_proc = 40
    bound = 300  # crossed several times over the run given short lines
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(
            target=_child_append_with_small_bound,
            args=(str(sink), n_per_proc, idx, bound),
        )
        for idx in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    all_lines: list[bytes] = []
    if sink.exists():
        all_lines.extend(sink.read_bytes().splitlines(keepends=True))
    for rotated in sorted(tmp_path.glob("touch-record.jsonl.rotated-*")):
        all_lines.extend(rotated.read_bytes().splitlines(keepends=True))

    seen_by_idx = {idx: 0 for idx in range(n_procs)}
    for line in all_lines:
        event = decode_line(line)  # raises if interleave-corrupted
        idx = int(event.session_id.rsplit("-", 1)[1])
        seen_by_idx[idx] += 1

    assert seen_by_idx == {idx: n_per_proc for idx in range(n_procs)}


def test_compact_record_keeps_last_verb_wins_projection(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="b.py")
    append_event(sink, session_id="sid-1", agent_id=None, verb="R", path="a.py")

    compact_record(sink)

    lines = sink.read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    events = {decode_line(line).path: decode_line(line).verb for line in lines}
    assert events == {"a.py": "R", "b.py": "T"}


def test_compact_record_drops_malformed_lines_without_aborting(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with open(sink, "ab") as f:
        f.write(b"not valid json\n")
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="b.py")

    compact_record(sink)

    lines = sink.read_bytes().splitlines(keepends=True)
    paths = {decode_line(line).path for line in lines}
    assert paths == {"a.py", "b.py"}


def test_trailing_unterminated_line_is_dropped_not_flagged(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with open(sink, "ab") as f:
        f.write(b'{"v": 1, "verb": "T", "ts": 1.0, "sid": "sid-1"')  # no closing, no newline

    compact_record(sink)  # must not raise MalformedRecordLine

    lines = sink.read_bytes().splitlines(keepends=True)
    assert len(lines) == 1
    assert decode_line(lines[0]).path == "a.py"


# ---------------------------------------------------------------------------
# C3: the read seam.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _all_sessions_live(monkeypatch):
    """Every test in this file is about the read seam's own merge/degrade
    logic, not liveness.session_live's decision tree (that module's own
    tests own that) -- fix session_live to True by default so a claim's
    presence in ``TouchProjection.claims`` reflects only this module's
    projection, not an incidental dead-session filter. Individual tests
    that need a dead session override this per-call."""
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: True
    )


def test_discover_family_orders_rotated_siblings_oldest_first_then_live(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    sink.write_bytes(b"live\n")
    older = tmp_path / "touch-record.jsonl.rotated-1000-111.jsonl"
    newer = tmp_path / "touch-record.jsonl.rotated-2000-222.jsonl"
    newer.write_bytes(b"newer\n")
    older.write_bytes(b"older\n")

    family = discover_family(sink)

    assert family == [older, newer, sink]


def test_discover_family_tie_breaks_same_timestamp_rotation_by_pid(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    low_pid = tmp_path / "touch-record.jsonl.rotated-5000-100.jsonl"
    high_pid = tmp_path / "touch-record.jsonl.rotated-5000-200.jsonl"
    high_pid.write_bytes(b"x\n")
    low_pid.write_bytes(b"x\n")

    family = discover_family(sink)

    assert family == [low_pid, high_pid]


def test_discover_family_handles_no_live_file(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    rotated = tmp_path / "touch-record.jsonl.rotated-1-1.jsonl"
    rotated.write_bytes(b"x\n")

    assert discover_family(sink) == [rotated]
    assert discover_family(tmp_path / "no-such-sink.jsonl") == []


def test_project_live_claims_folds_family_last_verb_wins_across_rotation(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    rotated = tmp_path / "touch-record.jsonl.rotated-1-1.jsonl"
    rotated.write_bytes(
        encode_line(session_id="sid-1", agent_id=None, verb="T", path="a.py", timestamp=1.0)
    )
    sink.write_bytes(
        encode_line(session_id="sid-1", agent_id=None, verb="R", path="a.py", timestamp=2.0)
    )

    projection = project_live_claims(sink)

    assert projection.claims == {}
    assert projection.degraded is False


def test_project_live_claims_reports_empty_and_not_degraded_when_nothing_claimed(tmp_path):
    sink = tmp_path / "no-such-sink.jsonl"

    projection = project_live_claims(sink)

    assert projection == TouchProjection(claims={}, degraded=False, degrade_reasons=())


def test_project_live_claims_filters_dead_session_claims(tmp_path, monkeypatch):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="dead-sid", agent_id=None, verb="T", path="a.py")
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: False
    )

    projection = project_live_claims(sink)

    assert projection.claims == {}


def test_project_live_claims_keeps_live_touch_claim(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")

    projection = project_live_claims(sink)

    assert set(projection.claims) == {"a.py"}
    assert projection.claims["a.py"].session_id == "sid-1"


def test_merge_across_streams_uses_timestamp_when_no_shared_byte_order(tmp_path):
    """An agent-keyed T sink and a session-keyed R sink for the same path
    live in two different files with no shared order -- ts decides."""
    t_sink = tmp_path / "agent-touch-record.jsonl"
    r_sink = tmp_path / "session-touch-record.jsonl"
    r_sink.write_bytes(
        encode_line(session_id="sid-1", agent_id=None, verb="R", path="a.py", timestamp=1.0)
    )
    t_sink.write_bytes(
        encode_line(session_id="sid-1", agent_id="scout@sid-1", verb="T", path="a.py", timestamp=2.0)
    )

    projection = project_live_claims(t_sink, r_sink)

    assert projection.claims["a.py"].verb == VERB_TOUCH


def test_merge_across_streams_tie_break_favors_touch_over_release(tmp_path):
    t_sink = tmp_path / "agent-touch-record.jsonl"
    r_sink = tmp_path / "session-touch-record.jsonl"
    r_sink.write_bytes(
        encode_line(session_id="sid-1", agent_id=None, verb="R", path="a.py", timestamp=5.0)
    )
    t_sink.write_bytes(
        encode_line(session_id="sid-1", agent_id="scout@sid-1", verb="T", path="a.py", timestamp=5.0)
    )

    projection_rt = project_live_claims(r_sink, t_sink)
    projection_tr = project_live_claims(t_sink, r_sink)

    assert projection_rt.claims["a.py"].verb == VERB_TOUCH
    assert projection_tr.claims["a.py"].verb == VERB_TOUCH


def test_unreadable_file_sets_degraded_typed_signal_not_empty_claims(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    # A directory at the sink's own path makes read_bytes() raise an
    # OSError (IsADirectoryError/PermissionError depending on platform),
    # simulating an unreadable family member without touching permissions.
    sink.mkdir()

    before = degrade_counts().get("unreadable_file", 0)
    projection = project_live_claims(sink)
    after = degrade_counts().get("unreadable_file", 0)

    assert projection.degraded is True
    assert projection.claims == {}
    assert after == before + 1


def test_malformed_complete_line_is_typed_degrade_not_silent_drop(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with open(sink, "ab") as f:
        f.write(b"not valid json\n")

    before = degrade_counts().get("malformed_line", 0)
    projection = project_live_claims(sink)
    after = degrade_counts().get("malformed_line", 0)

    assert projection.degraded is True
    assert set(projection.claims) == {"a.py"}  # what DID decode is still returned
    assert after == before + 1


def test_trailing_unterminated_line_never_sets_degraded_via_read_seam(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with open(sink, "ab") as f:
        f.write(b'{"v": 1, "verb": "T", "ts": 1.0, "sid": "sid-1"')  # no closing, no newline

    projection = project_live_claims(sink)

    assert projection.degraded is False
    assert set(projection.claims) == {"a.py"}


# ---------------------------------------------------------------------------
# C7a / AC23: containment lands at the encoder, spawn-free, REJECT posture.
# ---------------------------------------------------------------------------


def _explode_on_spawn(monkeypatch):
    """Fail the test loudly if anything under it spawns a subprocess."""
    import subprocess

    def _explode(*args, **kwargs):
        raise AssertionError("touch_record spawned a subprocess: %r" % (args,))

    monkeypatch.setattr(subprocess, "run", _explode)
    monkeypatch.setattr(subprocess, "Popen", _explode)
    monkeypatch.setattr(subprocess, "check_output", _explode)
    monkeypatch.setattr(subprocess, "check_call", _explode)


@pytest.mark.parametrize(
    "absolute_path",
    [
        "C:\\Users\\someone\\outside\\repo.py",  # abs-path-ok: synthetic containment-rejection fixture, not a real machine path
        "C:/Users/someone/outside/repo.py",
        "/etc/passwd",
        "//host/share/file.py",
    ],
)
def test_encode_line_rejects_out_of_worktree_path(absolute_path):
    with pytest.raises(OutOfWorktreePath):
        encode_line(session_id="sid-1", agent_id=None, verb="T", path=absolute_path)


def test_encode_line_does_not_reject_a_relative_upward_escape():
    """AC23's brief is an out-of-worktree ABSOLUTE path; a relative ``..``
    escape stays outside this check's scope on purpose --
    ``TestAC8DefensiveHistoricalNormalization`` (test_scope.py) writes
    exactly this shape through ``append_event`` to exercise AC8's own
    read-time defensive normalization of a poisoned peer entry, and this
    encoder must not break that already-covered contract."""
    encoded = encode_line(session_id="sid-1", agent_id=None, verb="T", path="../shared.py")
    event = decode_line(encoded)
    assert event.path == "../shared.py"


def test_append_event_rejects_out_of_worktree_path_and_writes_nothing(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    with pytest.raises(OutOfWorktreePath):
        append_event(
            sink, session_id="sid-1", agent_id=None, verb="T", path="C:\\outside\\repo.py",
        )
    assert not sink.exists()


def test_repo_relative_path_is_unaffected_by_the_containment_check(tmp_path):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a\\b\\c.py")
    event = decode_line(sink.read_bytes())
    assert event.path == "a/b/c.py"


def test_out_of_worktree_write_then_read_is_zero_spawn_drive_letter(tmp_path, monkeypatch):
    """f9's probe, promoted to a regression test: an out-of-worktree,
    drive-letter-absolute path written through ``append_event`` is REJECTED
    at write time (never stored), so the subsequent read via
    ``project_live_claims`` never has an absolute path to git-spawn against
    -- zero spawns on the read."""
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with pytest.raises(OutOfWorktreePath):
        append_event(
            sink,
            session_id="sid-1",
            agent_id=None,
            verb="T",
            path="D:\\outside\\worktree\\secret.py",
        )

    _explode_on_spawn(monkeypatch)
    projection = project_live_claims(sink)

    assert projection.degraded is False
    assert set(projection.claims) == {"a.py"}


def test_out_of_worktree_write_then_read_is_zero_spawn_posix_absolute(tmp_path, monkeypatch):
    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    with pytest.raises(OutOfWorktreePath):
        append_event(
            sink, session_id="sid-1", agent_id=None, verb="T", path="/etc/outside/secret.py",
        )

    _explode_on_spawn(monkeypatch)
    projection = project_live_claims(sink)

    assert projection.degraded is False
    assert set(projection.claims) == {"a.py"}


def test_missing_family_member_between_discover_and_read_is_benign(tmp_path, monkeypatch):
    """A peer's own rotation can race our directory listing -- a member
    ``discover_family`` just listed vanishes before ``read_bytes()`` runs.
    Never a degrade (module docstring's Failure posture section)."""
    from coordinator_core.session import touch_record as mod

    sink = tmp_path / "touch-record.jsonl"
    append_event(sink, session_id="sid-1", agent_id=None, verb="T", path="a.py")
    ghost = tmp_path / "touch-record.jsonl.rotated-1-1.jsonl"  # never created on disk

    monkeypatch.setattr(mod, "discover_family", lambda sink_path: [ghost, sink])

    before = degrade_counts()
    stream_claims, degraded, reasons = mod._read_stream_claims(sink)
    after = degrade_counts()

    assert degraded is False
    assert after == before
    assert set(stream_claims) == {"a.py"}


def test_same_process_double_rotation_in_one_millisecond_keeps_both_generations(
    tmp_path, monkeypatch
):
    """The mechanism behind the lost lines, pinned directly rather than through
    the concurrency test that found it.

    A rotated filename was `<name>.rotated-<ts_ms>-<pid>.jsonl`. One process
    crossing the bound twice inside the same millisecond built that name twice,
    and `os.replace` onto an existing path overwrites -- so the first rotated
    generation was destroyed with its events in it. `pid` tie-breaks two
    PROCESSES, which is what the module reasoned about; it cannot tie-break one
    process against itself.
    """
    monkeypatch.setattr(touch_record.time, "time", lambda: 1788000000.0)

    sink = tmp_path / "touch-record.jsonl"
    sink.write_bytes(b"generation-one\n")
    touch_record._rotate_oversized(sink)
    sink.write_bytes(b"generation-two\n")
    touch_record._rotate_oversized(sink)

    rotated = sorted(tmp_path.glob("touch-record.jsonl.rotated-*"))
    assert len(rotated) == 2, f"a generation was clobbered: {rotated!r}"
    assert {p.read_bytes() for p in rotated} == {b"generation-one\n", b"generation-two\n"}


def test_discover_family_orders_a_same_millisecond_double_rotation_chronologically(
    tmp_path, monkeypatch
):
    """The counter is monotonic so `discover_family` can still order one
    process's own generations correctly inside a millisecond -- which random
    tokens would not give."""
    monkeypatch.setattr(touch_record.time, "time", lambda: 1788000000.0)

    sink = tmp_path / "touch-record.jsonl"
    sink.write_bytes(b"first\n")
    touch_record._rotate_oversized(sink)
    sink.write_bytes(b"second\n")
    touch_record._rotate_oversized(sink)
    sink.write_bytes(b"live\n")

    family = touch_record.discover_family(sink)
    assert [p.read_bytes() for p in family] == [b"first\n", b"second\n", b"live\n"]


def test_discover_family_still_finds_generations_rotated_before_the_counter(tmp_path):
    """Files rotated under the two-component name are on disk right now. A
    legacy generation reads as seq 0, which orders it before any same-(ts, pid)
    generation written after it -- the correct order, since it was written
    first."""
    sink = tmp_path / "touch-record.jsonl"
    (tmp_path / "touch-record.jsonl.rotated-1788000000000-4242.jsonl").write_bytes(b"legacy\n")
    (tmp_path / "touch-record.jsonl.rotated-1788000000000-4242-1.jsonl").write_bytes(b"newer\n")
    sink.write_bytes(b"live\n")

    family = touch_record.discover_family(sink)
    assert [p.read_bytes() for p in family] == [b"legacy\n", b"newer\n", b"live\n"]
