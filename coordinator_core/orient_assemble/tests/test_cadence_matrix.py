"""
coordinator_core.orient_assemble.tests.test_cadence_matrix — C3 AC(a):
each cadence (session/day/week) must emit the correct directive/
judgment_point set. Cadence tunes severity/depth knobs over ONE shared
compute (Approach § "Cadence is a parameter, not three code paths") —
these tests isolate the cadence-sensitive branch point in each reader
family with monkeypatched siblings, so a fixture drift in one reader
can't mask a cadence regression in another.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C3

Negative-spec: does NOT invoke any reader's real I/O (git, disk, network,
subprocess) — every underlying read is monkeypatched to a deterministic
stub. Read-only-guarantee (AC-c) is asserted separately in
test_read_only_guarantee.py; this file's job is cadence branching only.
"""

from __future__ import annotations

import coordinator_core.daily_day as daily_day
import coordinator_core.ops.check_weekly_staleness as check_weekly_staleness
import coordinator_core.orient_assemble as orient_assemble
from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult


def _noop_isolate_clean_ops(monkeypatch, *, except_addon=False):
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda *, repo_root=None: ReaderResult())
    if not except_addon:
        monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))


def test_clean_ops_addon_health_mode_is_cadence_tuned(monkeypatch):
    calls: list[str] = []

    def fake_scan(mode):
        calls.append(mode)
        return [], 0

    _noop_isolate_clean_ops(monkeypatch, except_addon=True)
    monkeypatch.setattr(rco, "_scan_addon_health_run", fake_scan)

    rco.collect("day")
    rco.collect("session")
    rco.collect("week")

    assert calls == ["--red-and-stale", "--red-only", "--red-only"]


def test_clean_ops_runs_the_same_five_readers_for_every_cadence(monkeypatch):
    seen = {"em": 0, "memo": 0, "rag": 0, "worktree": 0}
    monkeypatch.setattr(
        rco, "_read_em_environment",
        lambda: (seen.__setitem__("em", seen["em"] + 1), ReaderResult())[1],
    )
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(
        rco, "_read_memo_surface",
        lambda mode, *, repo_root=None: (seen.__setitem__("memo", seen["memo"] + 1), ReaderResult())[1],
    )
    monkeypatch.setattr(
        rco, "_read_rag_staleness",
        lambda: (seen.__setitem__("rag", seen["rag"] + 1), ReaderResult())[1],
    )
    monkeypatch.setattr(
        rco, "_read_worktree_sweep",
        lambda *, repo_root=None: (seen.__setitem__("worktree", seen["worktree"] + 1), ReaderResult())[1],
    )

    for cadence in ("session", "day", "week"):
        rco.collect(cadence)

    assert seen == {"em": 3, "memo": 3, "rag": 3, "worktree": 3}


def test_health_reaper_dry_run_fires_on_day_cadence_only(monkeypatch):
    calls = {"reaper": 0}

    monkeypatch.setattr(rhr, "_read_claude_klabauter_bin_sentinel", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_working_repo_registration", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_ceremony_hook", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_marker_freshness", lambda cadence: ReaderResult())
    monkeypatch.setattr(
        rhr, "_read_reaper_dry_run",
        lambda repo_root=None: (calls.__setitem__("reaper", calls["reaper"] + 1), ReaderResult())[1],
    )

    rhr.collect("session")
    rhr.collect("week")
    assert calls["reaper"] == 0

    rhr.collect("day")
    assert calls["reaper"] == 1


def test_health_reaper_ceremony_hook_receives_the_cadence(monkeypatch):
    received: list[str] = []
    monkeypatch.setattr(rhr, "_read_claude_klabauter_bin_sentinel", lambda: ReaderResult())
    monkeypatch.setattr(rhr, "_read_working_repo_registration", lambda: ReaderResult())
    monkeypatch.setattr(
        rhr, "_read_ceremony_hook",
        lambda cadence: (received.append(cadence), ReaderResult())[1],
    )
    monkeypatch.setattr(rhr, "_read_marker_freshness", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_reaper_dry_run", lambda repo_root=None: ReaderResult())

    for cadence in ("session", "day", "week"):
        rhr.collect(cadence)

    assert received == ["session", "day", "week"]


def test_health_reaper_working_repo_registration_runs_every_cadence(monkeypatch):
    calls = {"working_repo": 0}
    monkeypatch.setattr(rhr, "_read_claude_klabauter_bin_sentinel", lambda: ReaderResult())
    monkeypatch.setattr(
        rhr, "_read_working_repo_registration",
        lambda: (calls.__setitem__("working_repo", calls["working_repo"] + 1), ReaderResult())[1],
    )
    monkeypatch.setattr(rhr, "_read_ceremony_hook", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_marker_freshness", lambda cadence: ReaderResult())
    monkeypatch.setattr(rhr, "_read_reaper_dry_run", lambda repo_root=None: ReaderResult())

    for cadence in ("session", "day", "week"):
        rhr.collect(cadence)

    assert calls["working_repo"] == 3


def test_marker_freshness_day_and_session_read_the_workday_marker_at_different_severities(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(check_weekly_staleness, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(daily_day, "local_day", lambda: "2026-07-24")

    marker = tmp_path / ".workday-start-marker"
    marker.write_text("2026-07-23", encoding="utf-8")

    day_result = rhr._read_marker_freshness("day")
    session_result = rhr._read_marker_freshness("session")

    assert [d["id"] for d in day_result.directives] == ["d-workday-marker-write"]
    assert session_result.directives == []
    assert day_result.judgment_points == []
    assert [jp["id"] for jp in session_result.judgment_points] == ["j-session-day-review-due"]


def test_marker_freshness_week_cadence_reads_header_not_the_day_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(check_weekly_staleness, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(check_weekly_staleness, "_compute_staleness", lambda text, root: "STALE")

    week_dir = tmp_path / "week-changelog"
    week_dir.mkdir()
    (week_dir / "HEADER.md").write_text("stub", encoding="utf-8")
    # A stale day marker must NOT leak into the week-cadence read.
    (tmp_path / ".workday-start-marker").write_text("1999-01-01", encoding="utf-8")

    result = rhr._read_marker_freshness("week")

    assert result.directives == []
    assert [jp["id"] for jp in result.judgment_points] == ["j-week-marker-freshness"]


def test_branch_reconcile_collect_is_cadence_insensitive(monkeypatch):
    monkeypatch.setattr(rbr, "_read_span_assert", lambda: ReaderResult())
    monkeypatch.setattr(rbr, "_read_auto_reconcile", lambda: ReaderResult())

    results = [rbr.collect(cadence) for cadence in ("session", "day", "week")]
    assert all(r == results[0] for r in results)


def test_handoff_triage_collect_is_cadence_insensitive(monkeypatch):
    monkeypatch.setattr(rht, "_read_stale_plans", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_ready", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_awaiting_gate", lambda: ReaderResult())
    monkeypatch.setattr(rht, "_read_orphaned_plans", lambda: ReaderResult())

    results = [rht.collect(cadence) for cadence in ("session", "day", "week")]
    assert all(r == results[0] for r in results)


def test_brief_forwards_repo_root_to_all_four_reader_collect_calls(monkeypatch):
    """C2 AC: `brief(cadence, *, repo_root=...)` forwards `repo_root` as a
    keyword to every reader family's `collect(cadence, repo_root=...)` —
    pure plumbing, no reader consumes it yet (C3-C7 do, one at a time)."""
    received: dict[str, tuple[str, str | None]] = {}

    def make_stub(name):
        def _stub(cadence, *, repo_root=None):
            received[name] = (cadence, repo_root)
            return ReaderResult()

        return _stub

    monkeypatch.setattr(rco, "collect", make_stub("clean_ops"))
    monkeypatch.setattr(rht, "collect", make_stub("handoff_triage"))
    monkeypatch.setattr(rbr, "collect", make_stub("branch_reconcile"))
    monkeypatch.setattr(rhr, "collect", make_stub("health_reaper"))

    orient_assemble.brief("day", repo_root="/some/repo")

    assert received == {
        "clean_ops": ("day", "/some/repo"),
        "handoff_triage": ("day", "/some/repo"),
        "branch_reconcile": ("day", "/some/repo"),
        "health_reaper": ("day", "/some/repo"),
    }
