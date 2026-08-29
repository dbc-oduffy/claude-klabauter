"""
coordinator_core.orient_assemble.tests.test_context_flood_caps — covers the
2026-07-30 context-flood fix: `brief('session')` shipped at 148 judgment
points / 124KB, ~91 of them one-per-inbound-cross-repo-memo (`j-memo-N`) and
~40 one-per-surfaced-handoff (`j-auto-reconcile-N`), both unbounded lists
that grow with disk contents.

Covers:
    - session-cadence memo suppression (zero `j-memo-*` entries, no depth
      count anywhere) per the `~/.claude/CLAUDE.md` ruling: "The cross-repo
      memo inbox doesn't move without deliberate Claude+human action. Depth
      is not a backlog and waiting memos are not overdue work — don't
      report the count."
    - the shared cap helper (`reader_result.cap_judgment_points`) binding
      and emitting exactly one overflow entry.
    - day/week cadence are unaffected by the memo suppression (memos still
      surface, capped).
    - the cap helper exists in exactly one place and drives both reader
      families (`readers_clean_ops`, `readers_branch_reconcile`).
    - `brief('session')`'s serialized byte size stays under a defended
      budget (see `test_brief_session_stays_under_byte_budget`).

Spec backlink: state/improvement-queue/2026-07-30-orientation-targets-work-finding-but-ems-d7e494b501a2.yaml
"""

from __future__ import annotations

import json

from coordinator_core.orient_assemble import brief
from coordinator_core.orient_assemble import readers_branch_reconcile as rbr
from coordinator_core.orient_assemble import readers_clean_ops as rco
from coordinator_core.orient_assemble.reader_result import ReaderResult, cap_judgment_points


def _make_judgment_points(n: int) -> list[dict]:
    return [
        {
            "id": f"j-fixture-{i + 1}",
            "question": f"fixture question {i + 1}?",
            "dispositions": [{"value": "leave_for_now", "resolves": []}],
            "evidence": "fixture evidence",
            "reason": "recommendation-forbidden",
            "recommendation": None,
            "revalidate_at_dispatch": True,
            "round_trip": "terminal",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# cap_judgment_points — the one shared helper both families call
# ---------------------------------------------------------------------------


def test_cap_helper_passes_through_when_under_cap():
    points = _make_judgment_points(3)
    result = cap_judgment_points(
        points,
        cap=5,
        overflow_id="j-overflow",
        item_label="fixtures",
        list_command="fixture-cli",
    )
    assert result == points


def test_cap_helper_binds_and_emits_exactly_one_overflow_entry():
    points = _make_judgment_points(10)
    result = cap_judgment_points(
        points,
        cap=4,
        overflow_id="j-overflow",
        item_label="fixtures",
        list_command="fixture-cli",
    )
    assert len(result) == 5  # 4 kept + exactly 1 overflow
    assert [jp["id"] for jp in result[:4]] == [p["id"] for p in points[:4]]
    overflow = result[-1]
    assert overflow["id"] == "j-overflow"
    assert "6" in overflow["evidence"]  # 10 - 4 = 6 withheld
    assert "fixture-cli" in overflow["evidence"]
    assert overflow["recommendation"] is None
    assert overflow["reason"] == "recommendation-forbidden"


def test_cap_helper_is_the_single_shared_implementation_for_both_families():
    """Both reader families must import the SAME function object — not two
    independently-written copies of the cap-and-overflow loop."""
    import coordinator_core.orient_assemble.readers_branch_reconcile as rbr_mod
    import coordinator_core.orient_assemble.readers_clean_ops as rco_mod

    assert rbr_mod.cap_judgment_points is cap_judgment_points
    assert rco_mod.cap_judgment_points is cap_judgment_points


# ---------------------------------------------------------------------------
# memo family — session suppression, day/week capped surfacing
# ---------------------------------------------------------------------------


def test_memo_surface_suppressed_entirely_at_session_cadence(monkeypatch):
    """mode="suppress" must short-circuit before any inbox read — zero JPs,
    no depth count anywhere (not even a summarizing single JP)."""
    monkeypatch.setattr(
        rco,
        "_resolve_inbox_dir",
        lambda cwd=None: (_ for _ in ()).throw(
            AssertionError("inbox dir must not be read when suppressed")
        ),
    )
    result = rco._read_memo_surface("suppress")
    assert result == ReaderResult()
    assert result.judgment_points == []


def test_memo_surface_collect_suppresses_only_at_session_cadence(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(rco, "_resolve_inbox_dir", lambda cwd=None: str(inbox))
    monkeypatch.setattr(rco, "_list_qualifying_lines", lambda d: ["memo line 1", "memo line 2"])
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda **kw: ReaderResult())

    session_result = rco.collect("session")
    day_result = rco.collect("day")
    week_result = rco.collect("week")

    assert [jp for jp in session_result.judgment_points if jp["id"].startswith("j-memo-")] == []
    assert len([jp for jp in day_result.judgment_points if jp["id"].startswith("j-memo-")]) == 2
    assert len([jp for jp in week_result.judgment_points if jp["id"].startswith("j-memo-")]) == 2


def test_collect_threads_repo_root_into_memo_surface_and_worktree_sweep(monkeypatch, tmp_path):
    """`collect(repo_root=...)` must reach `_read_memo_surface` and
    `_read_worktree_sweep` — the two readers whose underlying helpers
    already accept a `cwd` override."""
    seen_memo_root = []
    seen_worktree_root = []

    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: ([], 0))
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())

    def _fake_memo_surface(mode, *, repo_root=None):
        seen_memo_root.append(repo_root)
        return ReaderResult()

    def _fake_worktree_sweep(*, repo_root=None):
        seen_worktree_root.append(repo_root)
        return ReaderResult()

    monkeypatch.setattr(rco, "_read_memo_surface", _fake_memo_surface)
    monkeypatch.setattr(rco, "_read_worktree_sweep", _fake_worktree_sweep)

    passed_root = str(tmp_path / "caller-repo")
    rco.collect("day", repo_root=passed_root)

    assert seen_memo_root == [passed_root]
    assert seen_worktree_root == [passed_root]


def test_memo_surface_caps_at_day_cadence(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    lines = [f"memo line {i}" for i in range(rco._MEMO_JUDGMENT_POINT_CAP + 5)]
    monkeypatch.setattr(rco, "_resolve_inbox_dir", lambda cwd=None: str(inbox))
    monkeypatch.setattr(rco, "_list_qualifying_lines", lambda d: lines)

    result = rco._read_memo_surface("surface")

    # Overflow entry is `j-overflow-memo` (Review: code-reviewer — Finding 4)
    # — a distinct prefix from `j-memo-N`, so a naive `startswith("j-memo-")`
    # filter can no longer silently include it. No exclusion needed here.
    memo_jps = [jp for jp in result.judgment_points if jp["id"].startswith("j-memo-")]
    overflow_jps = [jp for jp in result.judgment_points if jp["id"] == "j-overflow-memo"]
    assert len(memo_jps) == rco._MEMO_JUDGMENT_POINT_CAP
    assert len(overflow_jps) == 1
    assert len(result.judgment_points) == rco._MEMO_JUDGMENT_POINT_CAP + 1


def test_memo_cap_keeps_action_required_over_newer_fyi(monkeypatch, tmp_path):
    """The cap must withhold the least-urgent memos, not an arbitrary tail.

    Band (`"0"` action-required, `"1"` fyi) outranks recency: a full cap's
    worth of fyi memos created today must NOT push a week-old
    action-required memo behind the overflow entry. Recency is only the
    tiebreak within a band.
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old_action = "0|2026-07-23|sibling|old but action-required|ask"
    fresh_fyis = [
        f"1|2026-07-30|sibling|fresh fyi {i}|fyi"
        for i in range(rco._MEMO_JUDGMENT_POINT_CAP + 5)
    ]
    monkeypatch.setattr(rco, "_resolve_inbox_dir", lambda cwd=None: str(inbox))
    monkeypatch.setattr(rco, "_list_qualifying_lines", lambda d: fresh_fyis + [old_action])

    result = rco._read_memo_surface("surface")

    kept = [jp for jp in result.judgment_points if jp["id"].startswith("j-memo-")]
    assert any("old but action-required" in jp["question"] for jp in kept)
    assert result.judgment_points[0]["id"] == "j-memo-1"
    assert "old but action-required" in result.judgment_points[0]["question"]


def test_memo_cap_orders_recent_first_within_a_band(monkeypatch, tmp_path):
    """Within one band, ordering is most-recent-first."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    lines = [
        "0|2026-07-24|sibling|older ask|ask",
        "0|2026-07-30|sibling|newer ask|ask",
    ]
    monkeypatch.setattr(rco, "_resolve_inbox_dir", lambda cwd=None: str(inbox))
    monkeypatch.setattr(rco, "_list_qualifying_lines", lambda d: lines)

    result = rco._read_memo_surface("surface")

    questions = [jp["question"] for jp in result.judgment_points]
    assert "newer ask" in questions[0]
    assert "older ask" in questions[1]


def test_memo_surface_no_overflow_when_under_cap(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(rco, "_resolve_inbox_dir", lambda cwd=None: str(inbox))
    monkeypatch.setattr(rco, "_list_qualifying_lines", lambda d: ["only one memo"])

    result = rco._read_memo_surface("surface")

    assert [jp["id"] for jp in result.judgment_points] == ["j-memo-1"]


# ---------------------------------------------------------------------------
# auto-reconcile family — capped regardless of cadence
# ---------------------------------------------------------------------------


def test_auto_reconcile_caps_unbounded_surfaced_list(monkeypatch):
    surfaced = [
        {"handoff_id": f"h-{i}", "reason": "gate_eval verdict=surface", "evidence": "x"}
        for i in range(rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP + 7)
    ]
    def _fake_get_response():
        return {"result": {"surfaced": surfaced}}

    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    monkeypatch.setattr(check_auto_reconcile, "get_response", _fake_get_response)

    result = rbr._read_auto_reconcile()

    # Overflow entry is `j-overflow-auto-reconcile` (Review: code-reviewer —
    # Finding 4) — a distinct prefix from `j-auto-reconcile-N`, so a naive
    # `startswith("j-auto-reconcile-")` filter can no longer silently
    # include it. No exclusion needed here.
    reconcile_jps = [
        jp for jp in result.judgment_points if jp["id"].startswith("j-auto-reconcile-")
    ]
    overflow_jps = [
        jp for jp in result.judgment_points if jp["id"] == "j-overflow-auto-reconcile"
    ]
    assert len(reconcile_jps) == rbr._AUTO_RECONCILE_JUDGMENT_POINT_CAP
    assert len(overflow_jps) == 1


def test_auto_reconcile_no_overflow_when_under_cap(monkeypatch):
    surfaced = [{"handoff_id": "h-1", "reason": "gate_eval verdict=surface", "evidence": "x"}]

    def _fake_get_response():
        return {"result": {"surfaced": surfaced}}

    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    monkeypatch.setattr(check_auto_reconcile, "get_response", _fake_get_response)

    result = rbr._read_auto_reconcile()

    assert [jp["id"] for jp in result.judgment_points] == ["j-auto-reconcile-1"]


# ---------------------------------------------------------------------------
# byte budget — the diff's actual claim, pinned to a test (Review:
# code-reviewer — Finding 2)
# ---------------------------------------------------------------------------


def test_brief_session_stays_under_byte_budget():
    """Pins the 20KB budget that is this diff's entire justification.

    Commit `4f131b1b` collapsed `brief('session')` from 146 judgment
    points / 122KB to 7 JP / 19247 bytes — a one-time measurement quoted in
    the commit message but never asserted by a test, so nothing failed red
    if a future change silently drifted back toward flood.

    30000 bytes is NOT today's 19247-byte measurement restated: it is
    ~10.7KB (56%) of headroom above it, deliberately loose enough to
    tolerate ordinary environment-to-environment variance in the live
    session-cadence readers this test does not monkeypatch (addon-health
    line count, worktree count, RAG staleness detail) while still catching
    the two regression shapes this budget exists to defend against —
    someone widening a count cap (`_MEMO_JUDGMENT_POINT_CAP`,
    `_AUTO_RECONCILE_JUDGMENT_POINT_CAP`) back toward the original flood, or
    a reader starting to embed materially more text per judgment point than
    `reader_result.truncate_external_text` currently allows. If this test
    ever fails, the fix is almost never to raise this number — re-derive the
    byte cost of whatever grew and decide whether it belongs in the
    session-cadence brief at all.

    2026-08-13 attribution (state/bug-backlog/2026-08-13-session-brief-byte
    -budget-assertion-is-r-8733361330d6.yaml): this assertion went red at
    33664 bytes, ~12% over budget. Byte-attributed by `cli` across
    `brief('session')['directives']`: `workday-start-handoff-triage`
    23628 of 26297 total directive bytes — overwhelmingly its `ready`
    subcommand alone (109 lines / 19175 bytes), an uncapped query over
    every `ready_to_fire`/`open` handoff that grows with disk contents,
    unlike the memo/auto-reconcile judgment-point families
    `cap_judgment_points` already bounds. Diagnosis: shape (a), a reader
    grown past its cap — not shape (b), a stale constant. Fixed by adding
    `_READY_LINE_CAP`/`_AWAITING_GATE_LINE_CAP` +
    `_cap_rendered_lines` to `readers_handoff_triage.py` (post-hoc line
    cap on the already-rendered text, same discipline as
    `_suppress_live_ledger_claims` and `_UNRECOGNIZED_STATUS_LINE_CAP` —
    never touches the ported query/format logic). Post-fix measurement:
    15936 bytes. The 30000 constant was not touched.
    """
    envelope = brief("session")
    size = len(json.dumps(envelope).encode("utf-8"))
    assert size < 30000, (
        f"brief('session') serialized to {size} bytes, over the 30000-byte "
        "budget this test defends — see this test's own docstring before "
        "raising the threshold"
    )
