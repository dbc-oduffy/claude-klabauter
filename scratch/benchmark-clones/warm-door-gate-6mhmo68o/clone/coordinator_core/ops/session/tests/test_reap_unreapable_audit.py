"""
Guard tests for the observability half of the uuid-shape gate:
``session.audit_unreapable`` (``_handler_audit_unreapable`` /
``_collect_unreapable``) and the gate-rejection counter's symmetry across
BOTH loops in ``_reap_stale_sessions``.

The gap these close. D1 (docs/plans/2026-08-26-the-reaper-identifies-
sessions-positively.md) accepted, as a priced cost, that a positive
uuid-shape gate makes every non-uuid-shaped hub child permanently
unreapable, and fixed ``session.reap``'s reporting of that population at a
DEBUG count — "a count, not a list". Two things followed that nobody could
act on: the fail-closed liveness-error loop applied the same gate and
counted nothing, so under a liveness failure the population vanished
entirely; and even on the good path a count gives an operator diagnosing hub
growth no path to WHICH directory is stuck. The count stays a count (the
reap op's contract is unchanged and asserted here); the names come from a
separate read-only op.

Both fixtures below are planted COLD deliberately: the population under test
must be selected by NAME SHAPE alone, never by age — a fixture that only
holds because it is young would pass this file and still leave the real
population unnamed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from coordinator_core.ops.session import reap

_COLD_AGE = reap._SESSION_STALE_SECONDS + 3600

_UUID_1 = "11111111-1111-4111-8111-111111111111"
_UUID_2 = "22222222-2222-4222-8222-222222222222"


def _plant_cold_dir(sessions_dir: Path, name: str) -> Path:
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "x.txt"
    f.write_text("x\n", encoding="utf-8")
    when = time.time() - _COLD_AGE
    os.utime(f, (when, when))
    os.utime(d, (when, when))
    return d


def _hub_with_all_four_populations(tmp_path: Path) -> Path:
    """A hub holding: two real session dirs, two gate-rejected stores, and one
    dot-prefixed traversal skip."""
    sessions = tmp_path / "coordinator-sessions"
    _plant_cold_dir(sessions, _UUID_1)
    _plant_cold_dir(sessions, _UUID_2)
    _plant_cold_dir(sessions, "decisions")
    _plant_cold_dir(sessions, "hookperf-3ee8b3f4a1d1")
    _plant_cold_dir(sessions, ".archive")
    return sessions


def test_collect_names_exactly_the_gate_rejected_population(tmp_path):
    """The audit reports gate-rejected children BY NAME, and reports only
    those: uuid-shaped session dirs are reapable (not this population), and
    dot-prefixed entries are traversal skips the reaper never treats as
    candidates in the first place."""
    sessions = _hub_with_all_four_populations(tmp_path)

    rows = reap._collect_unreapable(sessions)

    assert [row["name"] for row in rows] == ["decisions", "hookperf-3ee8b3f4a1d1"]
    # Age is reported so an operator can rank what is stuck, but it is never
    # a membership test — both rows are cold and both are present.
    assert all(row["age_days"] >= 1.0 for row in rows), rows


def test_collect_agrees_with_what_the_reaper_actually_leaves(tmp_path):
    """The audit's predicate must not drift from the reaper's. Rather than
    re-asserting the regex, run the real reap over the same hub and check the
    audit names exactly the non-uuid survivors it left behind."""
    sessions = _hub_with_all_four_populations(tmp_path)

    reaped, _deferred, failed = reap._reap_stale_sessions(
        sessions, frozenset(), None, None
    )
    assert failed == [], (reaped, failed)
    assert sorted(reaped) == [_UUID_1, _UUID_2]

    survivors_on_disk = {
        d.name
        for d in sessions.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }
    assert {row["name"] for row in reap._collect_unreapable(sessions)} == survivors_on_disk


def test_absent_hub_reports_an_empty_population_not_an_error(tmp_path):
    """Matches the reaper's own ``sessions_dir.is_dir()`` early return — a hub
    that was never created is not a fault condition."""
    assert reap._collect_unreapable(tmp_path / "coordinator-sessions") == []


def test_op_handler_returns_names_and_count_and_mutates_nothing(tmp_path):
    sessions = _hub_with_all_four_populations(tmp_path)
    before = sorted(p.name for p in sessions.iterdir())
    # age_days is derived from st_mtime, so a name-listing alone would miss a
    # bug that touched a child's mtime without renaming/removing it.
    before_mtimes = {p.name: p.stat().st_mtime for p in sessions.iterdir()}

    result = asyncio.run(reap._handler_audit_unreapable({}, tmp_path))

    assert result["exit_code"] == 0
    assert result["count"] == 2
    assert [row["name"] for row in result["unreapable"]] == [
        "decisions",
        "hookperf-3ee8b3f4a1d1",
    ]
    assert result["sessions_dir"] == str(sessions)
    assert sorted(p.name for p in sessions.iterdir()) == before
    assert {p.name: p.stat().st_mtime for p in sessions.iterdir()} == before_mtimes
    assert not (sessions / ".last-reap").exists(), (
        "a read-only audit must not touch the cadence marker"
    )


def test_op_handler_reports_a_missing_repo_root_as_a_setup_error():
    result = asyncio.run(reap._handler_audit_unreapable({}, None))

    assert result["exit_code"] == 1
    assert result["unreapable"] == []
    assert result["count"] == 0


def test_fail_closed_loop_counts_gate_rejections_too(tmp_path, caplog):
    """The asymmetry itself: under a liveness error, ``_reap_stale_sessions``
    defers everything and reaps nothing, but it still applies the uuid-shape
    gate — and used to increment no counter, so the gate-rejected population
    left zero signal on exactly the path where an operator most needs one.

    Asserted on the shared emitter's log line (the op's own contract surface),
    not on an internal counter, so the guard survives a refactor of how the
    tally is threaded.
    """
    sessions = _hub_with_all_four_populations(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=reap._LOG.name):
        reaped, deferred, failed = reap._reap_stale_sessions(
            sessions, frozenset(), "resolve_live_session_ids blew up", None
        )

    assert (reaped, failed) == ([], [])
    assert sorted(entry["id"] for entry in deferred) == [_UUID_1, _UUID_2], (
        "the fail-closed loop must still defer only uuid-shaped session dirs"
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "uuid-shape gate rejected 2 non-session candidate(s)" in message
        for message in messages
    ), messages


def test_reap_envelope_still_reports_no_names(tmp_path):
    """D1's contract, held: widening ``session.reap``'s own output is the fix
    this design rejected. The names live in the audit op or nowhere."""
    sessions = _hub_with_all_four_populations(tmp_path)

    reaped, deferred, failed = reap._reap_stale_sessions(
        sessions, frozenset(), None, None
    )

    named = set(reaped) | {e["id"] for e in deferred} | {e["id"] for e in failed}
    assert "decisions" not in named
    assert "hookperf-3ee8b3f4a1d1" not in named
