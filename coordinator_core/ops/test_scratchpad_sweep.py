"""Tests for coordinator_core.ops.scratchpad_sweep (op scratchpad.sweep).

Covers: the UUID hard-name-filter (safety-critical — non-UUID siblings at
both the claude/ and session-dir layers must never be touched), the
liveness+age two-gate contract, the per-project-slug liveness scoping fix
(a slug must resolve to exactly one known repo root before liveness is even
consulted — an unmapped or collided slug is "undeterminable", never
reclaimable), dry-run-by-default, self-session exclusion, per-directory
error isolation, and the JSON-RPC param-validation surface.

Liveness is monkeypatched at ``coordinator_core.session.liveness.session_live``
rather than exercised against a real coordinator-sessions registry — this
module's own liveness call is a thin, direct pass-through, so pinning its
return value is the correct unit boundary; a real-registry integration is
coverage for ``coordinator_core.session.liveness`` itself, not for this
sweep. Likewise, ``slug_to_root_map`` is always passed explicitly in these
tests — the real discovery path (``_build_slug_to_root_map`` /
``discover_working_repos`` Tier A + Tier A.5) depends on this machine's
actual ``~/.claude/projects/`` and registered repos, which is out of scope
for a unit boundary; ``test_build_slug_to_root_map_is_forward_only`` below is
the one test that exercises the encode-and-collide logic directly, without
touching real discovery.
"""
from __future__ import annotations

import time

import pytest

from coordinator_core.ops.scratchpad_sweep import (
    _apply_size_cut,
    _build_slug_to_root_map,
    _encode_project_slug,
    _handler,
    sweep_scratchpads,
)

_SID_LIVE = "11111111-1111-1111-1111-111111111111"
_SID_DEAD_RECENT = "22222222-2222-2222-2222-222222222222"
_SID_DEAD_OLD = "33333333-3333-3333-3333-333333333333"
_SID_SELF = "44444444-4444-4444-4444-444444444444"
_SID_NO_SCRATCHPAD = "55555555-5555-5555-5555-555555555555"

_ALL_SIDS = (_SID_LIVE, _SID_DEAD_RECENT, _SID_DEAD_OLD, _SID_SELF, _SID_NO_SCRATCHPAD)

_OLD_AGE_SECS = 10 * 86400  # 10 days — past the default 7-day TTL
_RECENT_AGE_SECS = 60  # 1 minute — well inside the default TTL

# Default fixed slug -> root map for tests that don't care about the mapping
# itself — the fixture's project-slug resolves to SOME known root so the
# liveness/age gates under test actually get exercised.
_DEFAULT_SLUG_MAP = {
    "X--claude-klabauter": "X:/claude-klabauter",
    "Y--other-project": "Y:/other-project",
}


def _age_file(path, age_secs):
    stamp = time.time() - age_secs
    import os

    os.utime(path, (stamp, stamp))


def _build_fixture(tmp_path, project_slug="X--claude-klabauter"):
    claude_root = tmp_path / "claude" / project_slug
    claude_root.mkdir(parents=True)

    for sid in _ALL_SIDS:
        sdir = claude_root / sid
        sdir.mkdir()
        if sid == _SID_NO_SCRATCHPAD:
            continue
        scratch = sdir / "scratchpad"
        scratch.mkdir()
        f = scratch / "a.txt"
        f.write_text("x" * 100, encoding="utf-8")
        if sid == _SID_DEAD_OLD:
            _age_file(f, _OLD_AGE_SECS)
        elif sid == _SID_DEAD_RECENT:
            _age_file(f, _RECENT_AGE_SECS)

    # Non-UUID siblings — must never be touched, at either enumeration layer.
    (tmp_path / "pytest-of-example-operator").mkdir(exist_ok=True)
    (tmp_path / "repro").mkdir(exist_ok=True)
    (claude_root / "not-a-uuid-dir").mkdir()

    return tmp_path


def _fake_session_live(sid, cwd=None):
    return sid == _SID_LIVE


@pytest.fixture(autouse=True)
def _patch_liveness(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._session_liveness.session_live",
        _fake_session_live,
    )


def _sweep(tmp_path, slug_to_root_map=_DEFAULT_SLUG_MAP, **kwargs):
    kwargs.setdefault("self_session_id", _SID_SELF)
    return sweep_scratchpads(
        temp_root=str(tmp_path), slug_to_root_map=slug_to_root_map, **kwargs
    )


def _entry_for(result, sid):
    matches = [e for e in result["entries"] if e["session_id"] == sid]
    assert len(matches) == 1, f"expected exactly one entry for {sid}, got {matches}"
    return matches[0]


def test_dry_run_is_default_and_mutates_nothing(tmp_path):
    _build_fixture(tmp_path)
    old_scratch = tmp_path / "claude" / "X--claude-klabauter" / _SID_DEAD_OLD / "scratchpad"
    assert old_scratch.is_dir()

    result = _sweep(tmp_path)

    assert result["reclaim"] is False
    assert old_scratch.is_dir(), "dry-run must never delete"
    assert _entry_for(result, _SID_DEAD_OLD)["verdict"] == "reclaimable"
    assert _entry_for(result, _SID_DEAD_OLD)["action"] == "preview"


def test_live_session_never_touched(tmp_path):
    _build_fixture(tmp_path)
    result = _sweep(tmp_path, reclaim=True)
    entry = _entry_for(result, _SID_LIVE)
    assert entry["verdict"] == "live"
    scratch = tmp_path / "claude" / "X--claude-klabauter" / _SID_LIVE / "scratchpad"
    assert scratch.is_dir()


def test_dead_but_recent_is_too_recent_not_reclaimed(tmp_path):
    _build_fixture(tmp_path)
    result = _sweep(tmp_path, reclaim=True)
    entry = _entry_for(result, _SID_DEAD_RECENT)
    assert entry["verdict"] == "too-recent"
    scratch = tmp_path / "claude" / "X--claude-klabauter" / _SID_DEAD_RECENT / "scratchpad"
    assert scratch.is_dir()


def test_dead_and_old_is_reclaimed_when_opted_in(tmp_path):
    _build_fixture(tmp_path)
    scratch = tmp_path / "claude" / "X--claude-klabauter" / _SID_DEAD_OLD / "scratchpad"
    assert scratch.is_dir()

    result = _sweep(tmp_path, reclaim=True)

    entry = _entry_for(result, _SID_DEAD_OLD)
    assert entry["verdict"] == "reclaimed"
    assert entry["action"] == "deleted"
    assert not scratch.exists()
    assert result["bytes_reclaimed"] >= 100


def test_own_session_never_reclaimed_even_if_dead_and_old(tmp_path):
    fixture_root = _build_fixture(tmp_path)
    self_scratch = fixture_root / "claude" / "X--claude-klabauter" / _SID_SELF / "scratchpad"
    # Age it well past the TTL — the self-check must win regardless.
    _age_file(self_scratch / "a.txt", _OLD_AGE_SECS)

    result = _sweep(tmp_path, reclaim=True)

    entry = _entry_for(result, _SID_SELF)
    assert entry["verdict"] == "self"
    assert self_scratch.is_dir()


def test_no_scratchpad_child_is_skipped_not_an_error(tmp_path):
    _build_fixture(tmp_path)
    result = _sweep(tmp_path)
    entry = _entry_for(result, _SID_NO_SCRATCHPAD)
    assert entry["verdict"] == "no-scratchpad"


def test_non_uuid_siblings_never_enumerated(tmp_path):
    _build_fixture(tmp_path)
    result = _sweep(tmp_path)
    seen_sids = {e["session_id"] for e in result["entries"]}
    assert "not-a-uuid-dir" not in seen_sids
    assert len(result["entries"]) == len(_ALL_SIDS)


def test_ttl_days_is_configurable(tmp_path):
    _build_fixture(tmp_path)
    # A 20-day TTL makes even the "old" (10-day) fixture too-recent.
    result = _sweep(tmp_path, ttl_days=20.0)
    entry = _entry_for(result, _SID_DEAD_OLD)
    assert entry["verdict"] == "too-recent"


def test_project_slugs_filter_restricts_scope(tmp_path):
    _build_fixture(tmp_path, project_slug="X--claude-klabauter")
    _build_fixture(tmp_path, project_slug="Y--other-project")

    result = _sweep(tmp_path, project_slugs=["X--claude-klabauter"])
    slugs = {e["project_slug"] for e in result["entries"]}
    assert slugs == {"X--claude-klabauter"}


def test_missing_claude_dir_returns_empty_report(tmp_path):
    result = _sweep(tmp_path)
    assert result["entries"] == []
    assert result["counts"] == {}


def test_one_bad_directory_does_not_sink_the_sweep(tmp_path, monkeypatch):
    _build_fixture(tmp_path)

    real_live = _fake_session_live

    def _raising_live(sid, cwd=None):
        if sid == _SID_DEAD_OLD:
            raise RuntimeError("simulated liveness failure")
        return real_live(sid, cwd)

    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._session_liveness.session_live",
        _raising_live,
    )

    result = _sweep(tmp_path, reclaim=True)

    # Fail-open: an exception from the liveness call reads as live, not
    # reclaimed — and every OTHER directory is still fully processed.
    bad_entry = _entry_for(result, _SID_DEAD_OLD)
    assert bad_entry["verdict"] == "live"
    assert _entry_for(result, _SID_LIVE)["verdict"] == "live"
    assert _entry_for(result, _SID_SELF)["verdict"] == "self"


def test_counts_are_consistent_with_entries(tmp_path):
    _build_fixture(tmp_path)
    result = _sweep(tmp_path)
    assert sum(result["counts"].values()) == len(result["entries"])


# ---------------------------------------------------------------------------
# Per-project-slug liveness scoping (the cross-repo false-dead fix)
# ---------------------------------------------------------------------------


def test_unmapped_slug_is_undeterminable_never_reclaimable(tmp_path):
    """A project-slug absent from the map must never be treated as dead —
    it must read undeterminable and never be reclaimed, even when dead+old
    and reclaim=True (the exact shape that deleted a live peer's tree)."""
    _build_fixture(tmp_path, project_slug="Z--unknown-project")
    scratch = tmp_path / "claude" / "Z--unknown-project" / _SID_DEAD_OLD / "scratchpad"
    assert scratch.is_dir()

    result = _sweep(
        tmp_path, slug_to_root_map={}, project_slugs=["Z--unknown-project"], reclaim=True
    )

    entry = _entry_for(result, _SID_DEAD_OLD)
    assert entry["verdict"] == "undeterminable"
    assert scratch.is_dir(), "an undeterminable slug must never be reclaimed"
    # session_live must never even be consulted for an unmapped slug.
    assert entry["live"] is None


def test_undeterminable_never_reclaimed_even_when_live_session_mocked_dead(tmp_path):
    """Belt-and-braces: even if the (unreachable) liveness mock would have said
    dead, an unmapped slug must short-circuit BEFORE session_live is called."""
    _build_fixture(tmp_path, project_slug="Z--unknown-project")

    result = _sweep(
        tmp_path, slug_to_root_map={}, project_slugs=["Z--unknown-project"], reclaim=True
    )
    for entry in result["entries"]:
        if entry["session_id"] in (_SID_SELF, _SID_NO_SCRATCHPAD):
            continue
        assert entry["verdict"] == "undeterminable"


def test_two_project_slugs_resolve_independent_liveness(tmp_path):
    """Cross-repo isolation: X's dead-old fixture reclaims under X's mapped
    root; an identical fixture under an UNMAPPED slug must not."""
    _build_fixture(tmp_path, project_slug="X--claude-klabauter")
    _build_fixture(tmp_path, project_slug="Z--unknown-project")

    result = _sweep(
        tmp_path,
        slug_to_root_map={"X--claude-klabauter": "X:/claude-klabauter"},
        reclaim=True,
    )

    mapped_entries = [
        e for e in result["entries"] if e["project_slug"] == "X--claude-klabauter"
    ]
    unmapped_entries = [
        e for e in result["entries"] if e["project_slug"] == "Z--unknown-project"
    ]
    assert any(e["verdict"] == "reclaimed" for e in mapped_entries)
    assert all(
        e["verdict"] == "undeterminable"
        for e in unmapped_entries
        if e["session_id"] not in (_SID_SELF, _SID_NO_SCRATCHPAD)
    )


def test_encode_project_slug_matches_harness_convention():
    assert _encode_project_slug("X:\\claude-klabauter") == "X--claude-klabauter"
    assert _encode_project_slug("X:/claude-klabauter") == "X--claude-klabauter"


def test_build_slug_to_root_map_drops_encoding_collisions(monkeypatch):
    """Two DIFFERENT discovered roots that encode to the SAME slug must be
    dropped from the map entirely (fail safe), never guessed toward either."""
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._known_repo_roots",
        lambda: ["X:/project.claude-klabauter", "X:/claude-klabauter"],
    )
    mapping = _build_slug_to_root_map()
    # Both encode to "X--claude-klabauter" — collision, dropped.
    assert "X--claude-klabauter" not in mapping


def test_build_slug_to_root_map_keeps_unambiguous_roots(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._known_repo_roots",
        lambda: ["X:/claude-klabauter", "X:/DoE-claude"],
    )
    mapping = _build_slug_to_root_map()
    assert mapping["X--claude-klabauter"] == "X:/claude-klabauter"
    assert mapping["X--DoE-claude"] == "X:/DoE-claude"


# ---------------------------------------------------------------------------
# JSON-RPC handler surface
# ---------------------------------------------------------------------------


def test_handler_default_dry_run(tmp_path, monkeypatch):
    _build_fixture(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._build_slug_to_root_map",
        lambda: _DEFAULT_SLUG_MAP,
    )
    result = _handler({"temp_root": str(tmp_path)})
    assert result["reclaim"] is False
    assert "entries" in result


@pytest.mark.parametrize(
    "bad_params",
    [
        {"reclaim": "yes"},
        {"ttl_days": "seven"},
        {"ttl_days": -1},
        {"ttl_days": True},
        {"temp_root": 123},
        {"project_slugs": "not-a-list"},
        {"project_slugs": [1, 2]},
    ],
)
def test_handler_invalid_params_structured_error(bad_params):
    result = _handler(bad_params)
    assert "error" in result


# ---------------------------------------------------------------------------
# Size-cut pass (additive to the TTL gate above)
# ---------------------------------------------------------------------------


def _make_dated_session(claude_root, sid, age_days, size_bytes=100):
    sdir = claude_root / sid
    sdir.mkdir()
    scratch = sdir / "scratchpad"
    scratch.mkdir()
    f = scratch / "a.txt"
    f.write_text("x" * size_bytes, encoding="utf-8")
    _age_file(f, age_days * 86400)


def test_size_cut_not_triggered_when_target_already_met(tmp_path):
    """Default 500 MB target with a tiny fixture — TTL behaviour unchanged,
    no cohorts pruned."""
    _build_fixture(tmp_path)
    result = _sweep(tmp_path, reclaim=True)

    assert result["size_cut"]["met"] is True
    assert result["size_cut"]["cohorts"] == []
    # Existing TTL behaviour: dead+old still reclaimed via the TTL gate alone.
    assert _entry_for(result, _SID_DEAD_OLD)["verdict"] == "reclaimed"


def test_size_cut_stops_at_right_threshold_whole_cohorts_only(tmp_path):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)

    sids = {
        6: "60000000-0000-0000-0000-000000000000",
        5: "50000000-0000-0000-0000-000000000000",
        4: "40000000-0000-0000-0000-000000000000",
        3: "30000000-0000-0000-0000-000000000000",
        2: "20000000-0000-0000-0000-000000000000",
        1: "10000000-0000-0000-0000-000000000000",
    }
    for day, sid in sids.items():
        _make_dated_session(claude_root, sid, day + 0.1, size_bytes=100)

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=250,
    )

    sc = result["size_cut"]
    # 600 total; prune 6,5,4 (100 each) -> remaining 300 > 250; prune 3 ->
    # remaining 200 <= 250: stop. 2 and 1 untouched.
    pruned_days = {c["age_days"] for c in sc["cohorts"] if c["pruned"]}
    assert pruned_days == {6, 5, 4, 3}
    assert sc["met"] is True
    assert sc["settled_at_age_days"] == 3

    for day in (6, 5, 4, 3):
        entry = _entry_for(result, sids[day])
        assert entry["verdict"] == "size-cut-reclaimed"
        assert entry["action"] == "deleted"
    for day in (2, 1):
        entry = _entry_for(result, sids[day])
        assert entry["verdict"] == "too-recent"


def test_size_cut_floor_reached_reports_shortfall_deletes_nothing_below_floor(tmp_path):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)

    sid_day3 = "30000000-0000-0000-0000-000000000001"
    sid_below_floor = "00000000-0000-0000-0000-000000000001"
    _make_dated_session(claude_root, sid_day3, 3.1, size_bytes=100)
    _make_dated_session(claude_root, sid_below_floor, 0.5, size_bytes=300)

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=100,
    )

    sc = result["size_cut"]
    assert sc["met"] is False
    assert sc["shortfall_bytes"] == 200
    assert "floor reached" in sc["shortfall_reason"]

    below_floor_scratch = claude_root / sid_below_floor / "scratchpad"
    assert below_floor_scratch.is_dir(), "nothing below the 1-day floor is ever eligible"
    entry = _entry_for(result, sid_below_floor)
    assert entry["verdict"] == "too-recent"


def test_size_cut_spares_live_directory_and_does_not_count_its_bytes(tmp_path, monkeypatch):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)

    sid_dead = "30000000-0000-0000-0000-000000000002"
    sid_live = "30000000-0000-0000-0000-000000000003"
    sid_below_floor = "30000000-0000-0000-0000-000000000005"
    _make_dated_session(claude_root, sid_dead, 3.1, size_bytes=100)
    _make_dated_session(claude_root, sid_live, 3.1, size_bytes=100)
    # Below the 1-day floor — never eligible, keeps the remainder above
    # target after the pruneable cohort is exhausted so this test can assert
    # an unmet shortfall alongside the spared live directory.
    _make_dated_session(claude_root, sid_below_floor, 0.5, size_bytes=300)

    def _live_only_one(sid, cwd=None):
        return sid == sid_live

    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._session_liveness.session_live",
        _live_only_one,
    )

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=1,
    )

    live_entry = _entry_for(result, sid_live)
    assert live_entry["verdict"] == "live"
    live_scratch = claude_root / sid_live / "scratchpad"
    assert live_scratch.is_dir()

    dead_entry = _entry_for(result, sid_dead)
    assert dead_entry["verdict"] == "size-cut-reclaimed"

    sc = result["size_cut"]
    assert sc["met"] is False
    assert "live/undeterminable directories were never sized" in sc["shortfall_reason"]


def test_size_cut_dry_run_full_accounting_no_deletion(tmp_path):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)

    sid = "30000000-0000-0000-0000-000000000004"
    _make_dated_session(claude_root, sid, 3.1, size_bytes=100)

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=False,
        size_cut_target_bytes=1,
    )

    entry = _entry_for(result, sid)
    assert entry["verdict"] == "size-cut-reclaimable"
    assert entry["action"] == "preview"
    scratch = claude_root / sid / "scratchpad"
    assert scratch.is_dir(), "dry-run must never delete"

    sc = result["size_cut"]
    assert sc["cohorts"], "dry-run must still report the full cohort accounting"
    assert sc["bytes_reclaimable"] >= 100


# ---------------------------------------------------------------------------
# Size-cut arithmetic edge cases (targeted at _apply_size_cut directly — no
# filesystem/liveness plumbing needed for the pure floor/ttl/accounting math)
# ---------------------------------------------------------------------------


def _entry(sid, *, verdict, age_days, bytes_=100, path="unused"):
    return {
        "project_slug": "X--claude-klabauter",
        "session_id": sid,
        "path": path,
        "verdict": verdict,
        "live": None,
        "age_days": age_days,
        "bytes": bytes_,
        "action": "skip",
        "error": None,
    }


def test_fractional_floor_days_never_prunes_below_the_stated_floor():
    """floor_days=1.5 must never make an entry aged 1.0-1.499 eligible — the
    whole day==1 cohort straddles the floor, so it must be excluded entirely
    rather than included via floor(1.5)==1. Regression for the P1 finding:
    pre-fix, math.floor(floor_days) let the day==1 cohort through whole."""
    entries = [
        _entry("a", verdict="too-recent", age_days=1.2, bytes_=100),
        _entry("b", verdict="too-recent", age_days=1.8, bytes_=100),
    ]
    report = _apply_size_cut(
        entries, ttl_days=7.0, reclaim=True, target_bytes=0, floor_days=1.5
    )
    assert entries[0]["verdict"] == "too-recent", "aged 1.2 is younger than the 1.5 floor"
    assert entries[1]["verdict"] == "too-recent", "day==1 cohort excluded wholesale at the floor"
    assert report["cohorts"] == [] or all(
        c["age_days"] != 1 for c in report["cohorts"]
    )


def test_fractional_ttl_days_still_visits_the_boundary_cohort(tmp_path):
    """ttl_days=7.5 must still let the size cut visit the day==7 cohort
    (ages 7.0-7.499, legitimately too-recent) — pre-fix, starting the loop at
    ttl_floor - 1 skipped it permanently regardless of target/floor."""
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    (a_dir / "f.txt").write_text("x" * 100, encoding="utf-8")

    entries = [_entry("a", verdict="too-recent", age_days=7.2, bytes_=100, path=str(a_dir))]
    report = _apply_size_cut(
        entries, ttl_days=7.5, reclaim=True, target_bytes=0, floor_days=1.0
    )
    assert entries[0]["verdict"] == "size-cut-reclaimed"
    assert any(c["age_days"] == 7 for c in report["cohorts"])


def test_partial_cohort_delete_failure_is_not_subtracted_from_remaining(tmp_path):
    """One entry's rmtree failing mid-cohort must not have its bytes
    subtracted from `remaining` — it's still on disk. Pre-fix, `remaining -=
    cohort_bytes` used the pre-computed cohort total regardless of which
    entries actually deleted."""
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    (ok_dir / "f.txt").write_text("x" * 100, encoding="utf-8")
    missing_dir = tmp_path / "does-not-exist"

    entries = [
        _entry("ok", verdict="too-recent", age_days=3.5, bytes_=100, path=str(ok_dir)),
        _entry(
            "fails", verdict="too-recent", age_days=3.5, bytes_=200, path=str(missing_dir)
        ),
    ]
    report = _apply_size_cut(
        entries, ttl_days=7.0, reclaim=True, target_bytes=50, floor_days=1.0
    )

    assert entries[0]["verdict"] == "size-cut-reclaimed"
    assert entries[1]["verdict"] == "error"
    # Only the successfully-deleted 100 bytes were subtracted — the failed
    # entry's 200 bytes are still real, on-disk usage.
    assert report["remaining_after_size_cut"] == 300 - 100
    assert report["met"] is False


def test_apply_size_cut_own_filter_excludes_live_verdict_even_with_age_days(tmp_path):
    """Targeted pin on _apply_size_cut's own cohort filter: a "live"-verdict
    entry that (hypothetically) carries a non-None age_days must still be
    excluded by the verdict half of the filter, not merely by age_days being
    None (which is all the upstream-liveness-gate-only fixture pinned)."""
    dead_dir = tmp_path / "dead"
    dead_dir.mkdir()
    (dead_dir / "f.txt").write_text("x" * 100, encoding="utf-8")

    entries = [
        _entry("live-with-age", verdict="live", age_days=5.0, bytes_=100, path="unused"),
        _entry("dead", verdict="too-recent", age_days=5.0, bytes_=100, path=str(dead_dir)),
    ]
    report = _apply_size_cut(
        entries, ttl_days=7.0, reclaim=True, target_bytes=0, floor_days=1.0
    )
    assert entries[0]["verdict"] == "live", "live entries must never be size-cut eligible"
    assert entries[1]["verdict"] == "size-cut-reclaimed"


# ---------------------------------------------------------------------------
# Per-file size predicate (2026-08-16) — a directory whose largest single
# file is >= size_cut_large_file_bytes uses size_cut_large_file_floor_days
# as its floor instead of size_cut_floor_days. Fixture-only, small explicit
# thresholds throughout (never the real 256 MB / 0.5-day production
# defaults, which would require multi-hundred-MB fixture files) so these
# tests stay fast — the defaults themselves are exercised only by AC8's
# manual dry-run against the real temp root, not by the automated tier.
# ---------------------------------------------------------------------------


def test_large_file_directory_eligible_at_the_shorter_floor(tmp_path):
    """AC2: a directory carrying a file >= size_cut_large_file_bytes becomes
    size-cut-eligible from size_cut_large_file_floor_days, well before it
    would clear the ordinary size_cut_floor_days."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid_large = "80000000-0000-0000-0000-000000000001"
    # Aged 0.7d: past the 0.5d large-file floor, short of the 1.0d ordinary
    # floor — the exact gap this predicate exists to reach into.
    _make_dated_session_with_files(claude_root, sid_large, 0.7, {"staging.db": 2000})

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=0,
        size_cut_floor_days=1.0,
        size_cut_large_file_bytes=1000,
        size_cut_large_file_floor_days=0.5,
    )

    entry = _entry_for(result, sid_large)
    assert entry["largest_file_bytes"] == 2000
    assert entry["verdict"] == "size-cut-reclaimed"
    scratch = claude_root / sid_large / "scratchpad"
    assert not scratch.exists()


def test_small_file_directory_same_age_is_not_eligible(tmp_path):
    """AC2, second half: "nothing changes for a directory whose largest file
    is under the threshold" — same age as the large-file case above, but its
    largest file is below size_cut_large_file_bytes, so it stays governed by
    the ordinary (unreached) size_cut_floor_days and is untouched."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid_small = "80000000-0000-0000-0000-000000000002"
    _make_dated_session_with_files(claude_root, sid_small, 0.7, {"note.txt": 200})

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=0,
        size_cut_floor_days=1.0,
        size_cut_large_file_bytes=1000,
        size_cut_large_file_floor_days=0.5,
    )

    entry = _entry_for(result, sid_small)
    assert entry["largest_file_bytes"] == 200
    assert entry["verdict"] == "too-recent"
    scratch = claude_root / sid_small / "scratchpad"
    assert scratch.is_dir()


def test_live_directory_with_large_file_never_selected_at_any_threshold(tmp_path, monkeypatch):
    """AC3: liveness gating stays unconditionally upstream of the per-file
    predicate — a live entry must never be selected, even at the most
    aggressive large-file thresholds (near-zero size, zero floor)."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid_live = "80000000-0000-0000-0000-000000000003"
    _make_dated_session_with_files(claude_root, sid_live, 5.0, {"huge.bin": 5000})

    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._session_liveness.session_live",
        lambda sid, cwd=None: sid == sid_live,
    )

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=0,
        size_cut_floor_days=1.0,
        size_cut_large_file_bytes=1,
        size_cut_large_file_floor_days=0.0,
    )

    entry = _entry_for(result, sid_live)
    assert entry["verdict"] == "live"
    scratch = claude_root / sid_live / "scratchpad"
    assert scratch.is_dir(), "a live directory must never be size-cut eligible"


def test_apply_size_cut_large_file_floor_never_selects_live_verdict():
    """AC3 at the _apply_size_cut unit boundary directly: a "live"-verdict
    entry carrying a largest_file_bytes above threshold and a non-None
    age_days must still never be size-cut eligible — the verdict filter
    alone must exclude it, at any large-file threshold."""
    entries = [
        _entry("live-large", verdict="live", age_days=5.0, bytes_=5000, path="unused"),
    ]
    entries[0]["largest_file_bytes"] = 999_999_999
    report = _apply_size_cut(
        entries,
        ttl_days=7.0,
        reclaim=True,
        target_bytes=0,
        floor_days=1.0,
        large_file_bytes=1,
        large_file_floor_days=0.0,
    )
    assert entries[0]["verdict"] == "live"
    assert report["bytes_reclaimed"] == 0


def test_large_file_floor_above_ordinary_floor_still_gates_the_upper_cohort(tmp_path):
    """Review: coordinator:code-reviewer P2 — when large_file_floor_days
    exceeds floor_days, the day >= floor_int branch must still exclude a
    large-file entry that hasn't reached its own (higher) floor. Pre-fix,
    reaching floor_int admitted the whole cohort unconditionally, deleting
    a large-file directory before its stated floor."""
    large_dir = tmp_path / "large"
    large_dir.mkdir()
    (large_dir / "f.bin").write_text("x" * 100, encoding="utf-8")

    entries = [
        _entry("large", verdict="too-recent", age_days=1.5, bytes_=100, path=str(large_dir)),
    ]
    entries[0]["largest_file_bytes"] = 5000
    report = _apply_size_cut(
        entries,
        ttl_days=7.0,
        reclaim=True,
        target_bytes=0,
        floor_days=1.0,
        large_file_bytes=1000,
        large_file_floor_days=2.0,
    )
    assert entries[0]["verdict"] == "too-recent", (
        "aged 1.5 is past the ordinary 1.0 floor but short of its own 2.0 "
        "large-file floor — must not be size-cut eligible"
    )
    assert report["bytes_reclaimed"] == 0


def test_large_file_floor_above_ordinary_floor_boundary_is_eligible(tmp_path):
    """Same inverted-floor configuration, but aged exactly at (and past) the
    higher large_file_floor_days — must become eligible once it clears its
    own floor, on the day >= floor_int path."""
    large_dir = tmp_path / "large"
    large_dir.mkdir()
    (large_dir / "f.bin").write_text("x" * 100, encoding="utf-8")

    entries = [
        _entry("large", verdict="too-recent", age_days=2.5, bytes_=100, path=str(large_dir)),
    ]
    entries[0]["largest_file_bytes"] = 5000
    report = _apply_size_cut(
        entries,
        ttl_days=7.0,
        reclaim=True,
        target_bytes=0,
        floor_days=1.0,
        large_file_bytes=1000,
        large_file_floor_days=2.0,
    )
    assert entries[0]["verdict"] == "size-cut-reclaimed"
    assert report["bytes_reclaimed"] == 100


def test_large_file_floor_equal_to_ordinary_floor_is_eligible(tmp_path):
    """Equal-value boundary: large_file_floor_days == floor_days is the
    shipped-defaults ordering's edge case — a large-file entry at exactly
    the shared floor must still be eligible via the day >= floor_int path."""
    large_dir = tmp_path / "large"
    large_dir.mkdir()
    (large_dir / "f.bin").write_text("x" * 100, encoding="utf-8")

    entries = [
        _entry("large", verdict="too-recent", age_days=1.5, bytes_=100, path=str(large_dir)),
    ]
    entries[0]["largest_file_bytes"] = 5000
    report = _apply_size_cut(
        entries,
        ttl_days=7.0,
        reclaim=True,
        target_bytes=0,
        floor_days=1.0,
        large_file_bytes=1000,
        large_file_floor_days=1.0,
    )
    assert entries[0]["verdict"] == "size-cut-reclaimed"
    assert report["bytes_reclaimed"] == 100


def test_fractional_floor_days_with_larger_large_file_floor_gates_correctly(tmp_path):
    """Fractional floor_days paired with a larger large_file_floor_days —
    the exact combination the reviewer named as untested. An entry past the
    fractional ordinary floor but short of the larger large-file floor must
    stay excluded; one past both must become eligible."""
    short_dir = tmp_path / "short"
    short_dir.mkdir()
    (short_dir / "f.bin").write_text("x" * 100, encoding="utf-8")
    long_dir = tmp_path / "long"
    long_dir.mkdir()
    (long_dir / "f.bin").write_text("x" * 100, encoding="utf-8")

    entries = [
        _entry("short", verdict="too-recent", age_days=1.6, bytes_=100, path=str(short_dir)),
        _entry("long", verdict="too-recent", age_days=2.6, bytes_=100, path=str(long_dir)),
    ]
    entries[0]["largest_file_bytes"] = 5000
    entries[1]["largest_file_bytes"] = 5000
    report = _apply_size_cut(
        entries,
        ttl_days=7.0,
        reclaim=True,
        target_bytes=0,
        floor_days=1.5,
        large_file_bytes=1000,
        large_file_floor_days=2.5,
    )
    assert entries[0]["verdict"] == "too-recent", (
        "aged 1.6 clears the fractional 1.5 ordinary floor but not the 2.5 "
        "large-file floor"
    )
    assert entries[1]["verdict"] == "size-cut-reclaimed", (
        "aged 2.6 clears both floors"
    )
    assert report["bytes_reclaimed"] == 100


def test_size_cut_large_file_params_flow_through_the_op_handler(tmp_path, monkeypatch):
    """AC1: size_cut_large_file_bytes/size_cut_large_file_floor_days are
    accepted, keyword-only-defaulted params on the op handler surface, not
    just on sweep_scratchpads directly."""
    _build_fixture(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.ops.scratchpad_sweep._build_slug_to_root_map",
        lambda: _DEFAULT_SLUG_MAP,
    )
    result = _handler(
        {
            "temp_root": str(tmp_path),
            "size_cut_large_file_bytes": 1000,
            "size_cut_large_file_floor_days": 0.25,
        }
    )
    assert "error" not in result
    assert result["size_cut"]["floor_days"] == 1.0  # ordinary floor unaffected


@pytest.mark.parametrize(
    "bad_params",
    [
        {"size_cut_large_file_bytes": "big"},
        {"size_cut_large_file_bytes": -1},
        {"size_cut_large_file_bytes": True},
        {"size_cut_large_file_floor_days": "half"},
        {"size_cut_large_file_floor_days": -1},
        {"size_cut_large_file_floor_days": True},
    ],
)
def test_handler_invalid_large_file_params_structured_error(bad_params):
    result = _handler(bad_params)
    assert "error" in result


def test_registered_under_op_key():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("scratchpad.sweep") is _handler


# ---------------------------------------------------------------------------
# Archive-shaped exemption (size-cut-scoped only — TTL gate unaffected)
# ---------------------------------------------------------------------------


def _make_dated_session_with_files(claude_root, sid, age_days, files):
    """Like `_make_dated_session` but takes {filename: size_bytes} so a
    fixture can drop an archive-shaped file alongside (or instead of) a
    plain one."""
    sdir = claude_root / sid
    sdir.mkdir()
    scratch = sdir / "scratchpad"
    scratch.mkdir()
    for fname, size_bytes in files.items():
        f = scratch / fname
        f.write_text("x" * size_bytes, encoding="utf-8")
        _age_file(f, age_days * 86400)


def test_archive_shaped_file_is_detected_and_totaled(tmp_path):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid = "70000000-0000-0000-0000-000000000001"
    _make_dated_session_with_files(
        claude_root, sid, 3.1, {"build.tar.zst": 500, "notes.txt": 20}
    )

    result = _sweep(
        tmp_path, slug_to_root_map={slug: "X:/claude-klabauter"}, project_slugs=[slug]
    )

    entry = _entry_for(result, sid)
    assert entry["archive_count"] == 1
    assert entry["archive_bytes"] == 500
    assert len(entry["archives"]) == 1
    assert entry["archives"][0]["path"].endswith("build.tar.zst")
    assert entry["bytes"] == 520


@pytest.mark.parametrize(
    "fname,is_archive",
    [
        ("build.tar.zst", True),
        ("build.tgz", True),
        ("build.zip", True),
        ("build.ZIP", True),
        ("plain.db", False),
        ("plain.json", False),
        ("plain.log", False),
    ],
)
def test_archive_shape_pattern_set(tmp_path, fname, is_archive):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid = "70000000-0000-0000-0000-000000000002"
    _make_dated_session_with_files(claude_root, sid, 3.1, {fname: 50})

    result = _sweep(
        tmp_path, slug_to_root_map={slug: "X:/claude-klabauter"}, project_slugs=[slug]
    )

    entry = _entry_for(result, sid)
    assert (entry["archive_count"] == 1) is is_archive


def test_too_recent_entry_with_archive_is_no_longer_size_cut_exempt(tmp_path):
    """REGRESSION-DIRECTION test for a deliberately reversed decision — do
    NOT "fix" this back. Through 2026-08-11 an archive-shaped file exempted
    its directory from the size-cut pass; the PM reversed that on
    2026-08-16 (see scratchpad_sweep.py's module docstring, "Archive-shaped
    exemption"). An archive-carrying "too-recent" entry must now be
    size-cut exactly like any other same-cohort entry — carrying an archive
    confers no special treatment here anymore. The stderr "no silent
    reclaim" warning (see test_archive_past_ttl_is_still_reclaimed_by_ttl_gate
    and the TTL-gate-scoped warning test below) is the archive class's only
    remaining protection."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)

    sid_archive = "70000000-0000-0000-0000-000000000003"
    sid_plain = "70000000-0000-0000-0000-000000000004"
    # Same cohort (day 3), same size — both must be size-cut identically now
    # that the archive-shaped exemption has fallen.
    _make_dated_session_with_files(claude_root, sid_archive, 3.1, {"release.tar.gz": 100})
    _make_dated_session_with_files(claude_root, sid_plain, 3.1, {"a.txt": 100})

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
        size_cut_target_bytes=0,
    )

    archive_entry = _entry_for(result, sid_archive)
    assert archive_entry["verdict"] == "size-cut-reclaimed"
    assert archive_entry["size_cut_exempt"] is False
    assert archive_entry["size_cut_exempt_reason"] is None
    scratch_archive = claude_root / sid_archive / "scratchpad"
    assert not scratch_archive.exists(), "archive-shaped file no longer blocks size-cut reclaim"

    plain_entry = _entry_for(result, sid_plain)
    assert plain_entry["verdict"] == "size-cut-reclaimed"
    assert plain_entry["size_cut_exempt"] is False

    # The exemption bookkeeping keys survive (report-shape stability) but the
    # exemption itself never fires anymore.
    sc = result["size_cut"]
    assert sc["archive_exempt_entries"] == 0
    assert sc["archive_exempt_bytes"] == 0


def test_stderr_warning_still_fires_for_archive_taken_by_ttl_gate(tmp_path, capsys):
    """The named stderr "no silent reclaim" line is the archive class's SOLE
    remaining protection post-reversal — it must still fire when the TTL
    gate (unaffected by the size-cut exemption reversal) reclaims an
    archive-carrying directory."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid = "70000000-0000-0000-0000-000000000008"
    _make_dated_session_with_files(claude_root, sid, 10, {"release.tar.zst": 100})

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
    )

    entry = _entry_for(result, sid)
    assert entry["verdict"] == "reclaimed"
    captured = capsys.readouterr()
    assert "archive-shaped file" in captured.err
    assert sid in captured.err


def test_archive_past_ttl_is_still_reclaimed_by_ttl_gate(tmp_path):
    """Size-cut exemption is scoped to the size-cut pass only — an archive
    aged past ttl_days is still reclaimed by the (unaffected) TTL gate."""
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid = "70000000-0000-0000-0000-000000000005"
    _make_dated_session_with_files(claude_root, sid, 10, {"release.tar.zst": 100})

    result = _sweep(
        tmp_path,
        slug_to_root_map={slug: "X:/claude-klabauter"},
        project_slugs=[slug],
        reclaim=True,
    )

    entry = _entry_for(result, sid)
    assert entry["verdict"] == "reclaimed"
    scratch = claude_root / sid / "scratchpad"
    assert not scratch.exists()


def test_short_circuit_entries_carry_archive_keys(tmp_path):
    """self / live / no-scratchpad / undeterminable entries must never be
    missing the archive keys, even though _scan_dir never ran for them."""
    _build_fixture(tmp_path)
    result = _sweep(tmp_path)
    for entry in result["entries"]:
        assert "archives" in entry
        assert "archive_count" in entry
        assert "archive_bytes" in entry
        if entry["verdict"] in ("self", "live", "no-scratchpad"):
            assert entry["archive_count"] == 0
            assert entry["archives"] == []


def test_archives_seen_flat_list_sorted_by_bytes_desc(tmp_path):
    slug = "X--claude-klabauter"
    claude_root = tmp_path / "claude" / slug
    claude_root.mkdir(parents=True)
    sid_a = "70000000-0000-0000-0000-000000000006"
    sid_b = "70000000-0000-0000-0000-000000000007"
    _make_dated_session_with_files(claude_root, sid_a, 3.1, {"small.zip": 50})
    _make_dated_session_with_files(claude_root, sid_b, 3.1, {"big.tar.zst": 900})

    result = _sweep(
        tmp_path, slug_to_root_map={slug: "X:/claude-klabauter"}, project_slugs=[slug]
    )

    seen = result["archives_seen"]
    assert [a["bytes"] for a in seen] == sorted(
        [a["bytes"] for a in seen], reverse=True
    )
    assert {a["session_id"] for a in seen} == {sid_a, sid_b}
    top = seen[0]
    assert top["bytes"] == 900
    assert top["session_id"] == sid_b
    assert top["verdict"] == "too-recent"
