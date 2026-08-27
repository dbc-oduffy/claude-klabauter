"""
coordinator_core.workstream_complete.test_review_scale — the review-trail
floor stops scanning the whole corpus.

Spec backlink: docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md,
chunk C11. `_resolve_review_brightline_floor_kwargs` used to `json.load` every
`*.json` under `state/review-trail/` and `archive/review-trail/` (4,337 files,
420-430ms measured) on every mid-chain close where `session_start_time`
resolved, filtering by `session_id` only AFTER the load. MECHANISM CORRECTED
(F7, C11's own dispatch brief) — no new producer, no write-time index: the
writer (`coordinator_core.ops.review_trail_write`) already names every record
`{TIMESTAMP}-{SESSION_ID[:8]}.json`, so `_list_review_trail_paths_for_root`
now pre-filters candidate filenames via `os.scandir` (metadata only, never an
`open()` call per candidate) on a `-{sid[:8]}` substring BEFORE any file is
opened. The existing exact `session_id`-field check after load is KEPT,
unchanged, as the 8-char-collision guard.

These tests prove the OPEN COUNT this module pays scales with the calling
session's own candidate count, never with the whole corpus (AC4: "No op in
scope reads a whole corpus to answer a question about one artifact") — a
large decoy corpus must not increase the number of files actually opened.

Negative-spec: does NOT re-test `_resolve_review_brightline_floor_kwargs`'s
git-resolution behaviour (session_start_sha / chain_tip_sha / record-tip
selection) — that surface is already covered by
`test_review_brightline_floor_wiring.py` and is unchanged by this chunk
(mechanism-only: a read-side name filter replacing an unfiltered directory
walk, same downstream contract). This file owns the SCAN-COST claim only.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.workstream_complete as wsc

_SID = "4839fcc4-7544-4ca6-bb5f-2cf0977e4620"
_SID_SHORT = _SID[:8]
_FOREIGN_SID = "deadbeef-0000-0000-0000-000000000000"


def _write_record(
    trail_dir: Path,
    filename: str,
    *,
    session_id: str = _SID,
    sha_range: str = "aaaaaaa^..aaaaaaa",
) -> Path:
    trail_dir.mkdir(parents=True, exist_ok=True)
    path = trail_dir / filename
    path.write_text(
        json.dumps(
            {
                "sha_range": sha_range,
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "single-reviewer-ok",
                "diff_loc": 10,
                "session_id": session_id,
                "workstream": None,
                "reviewed_paths": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def _decoy_filename(index: int) -> str:
    # A real production filename shape (`{TIMESTAMP}-{SESSION_ID[:8]}.json`)
    # for a DIFFERENT session — every decoy is a plausible on-disk record,
    # never a synthetic name that would trivially miss the filter for an
    # unrelated reason.
    return f"2026-08-{(index % 27) + 1:02d}-{index:06d}-{_FOREIGN_SID[:8]}.json"


# ---------------------------------------------------------------------------
# `_list_review_trail_paths_for_root` — the name-filter primitive itself.
# ---------------------------------------------------------------------------


def test_name_filter_returns_only_this_sessions_own_candidate(tmp_path):
    live_dir = tmp_path / "state" / "review-trail"
    _write_record(live_dir, f"2026-08-21-000001-{_SID_SHORT}.json")
    _write_record(live_dir, _decoy_filename(1), session_id=_FOREIGN_SID)
    _write_record(live_dir, _decoy_filename(2), session_id=_FOREIGN_SID)

    paths = wsc._list_review_trail_paths_for_root(tmp_path, sid_short=_SID_SHORT)

    assert len(paths) == 1
    assert paths[0].endswith(f"{_SID_SHORT}.json")


def test_name_filter_scales_with_own_candidates_not_corpus_size(tmp_path):
    """A 500-decoy corpus (a stand-in for the real 4,337-file measurement)
    must not change the candidate count this function returns for a session
    that owns exactly two of them — the whole point of the fix."""
    live_dir = tmp_path / "state" / "review-trail"
    archive_dir = tmp_path / "archive" / "review-trail"
    for i in range(400):
        _write_record(live_dir, _decoy_filename(i), session_id=_FOREIGN_SID)
    for i in range(400, 500):
        _write_record(archive_dir, _decoy_filename(i), session_id=_FOREIGN_SID)
    _write_record(live_dir, f"2026-08-21-000001-{_SID_SHORT}.json")
    _write_record(archive_dir, f"2026-08-20-000001-{_SID_SHORT}.json")

    paths = wsc._list_review_trail_paths_for_root(tmp_path, sid_short=_SID_SHORT)

    assert len(paths) == 2


def test_name_filter_empty_sid_short_preserves_prior_unfiltered_behaviour(tmp_path):
    """`sid_short=""` (the default) is the escape hatch for any OTHER caller
    that still wants the whole-corpus listing — unchanged from before this
    chunk."""
    live_dir = tmp_path / "state" / "review-trail"
    _write_record(live_dir, _decoy_filename(1), session_id=_FOREIGN_SID)
    _write_record(live_dir, f"2026-08-21-000001-{_SID_SHORT}.json")

    paths = wsc._list_review_trail_paths_for_root(tmp_path)

    assert len(paths) == 2


def test_name_filter_missing_directories_degrade_to_empty_list(tmp_path):
    """Neither `state/review-trail/` nor `archive/review-trail/` exists yet
    (a fresh worktree) — must degrade to an empty list, never raise."""
    assert wsc._list_review_trail_paths_for_root(tmp_path, sid_short=_SID_SHORT) == []


def test_name_filter_non_json_entries_are_ignored(tmp_path):
    live_dir = tmp_path / "state" / "review-trail"
    live_dir.mkdir(parents=True)
    (live_dir / f"2026-08-21-000001-{_SID_SHORT}.txt").write_text("not json", encoding="utf-8")
    (live_dir / f"2026-08-21-000001-{_SID_SHORT}.json").write_text("{}", encoding="utf-8")

    paths = wsc._list_review_trail_paths_for_root(tmp_path, sid_short=_SID_SHORT)

    assert len(paths) == 1
    assert paths[0].endswith(".json")


# ---------------------------------------------------------------------------
# `_resolve_review_brightline_floor_kwargs` — the open-count claim end to end,
# git-free (session_start_sha/chain_tip_sha resolution monkeypatched away —
# not this file's own claim to prove, see module Negative-spec above).
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_floor_kwargs_open_count_bounded_by_own_records_not_corpus_size(monkeypatch, tmp_path):
    """The scan-cost claim, proven directly: with 300 foreign-session decoys
    on disk plus this session's own 2 records, `_resolve_review_brightline_
    floor_kwargs` must open exactly this session's own 2 files — never the
    302-file corpus — confirming AC4's "no op in scope reads a whole corpus"
    for this call site."""
    live_dir = tmp_path / "state" / "review-trail"
    for i in range(300):
        _write_record(live_dir, _decoy_filename(i), session_id=_FOREIGN_SID)
    _write_record(live_dir, f"2026-08-21-000001-{_SID_SHORT}.json", sha_range="aaaaaaa^..aaaaaaa")
    _write_record(live_dir, f"2026-08-21-000002-{_SID_SHORT}.json", sha_range="bbbbbbb^..bbbbbbb")

    monkeypatch.setattr(wsc, "_resolve_session_start_sha", lambda root, t: "cccccccccccccccccccccccccccccccccccccccc")
    monkeypatch.setattr(wsc, "_resolve_head_sha", lambda root: "dddddddddddddddddddddddddddddddddddddddd")

    real_open = builtins.open
    opened_paths: list[str] = []

    def _counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        path_str = str(file)
        if path_str.endswith(".json") and "review-trail" in path_str.replace("\\", "/"):
            opened_paths.append(path_str)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _counting_open)

    result = wsc._resolve_review_brightline_floor_kwargs(tmp_path, _SID, session_start_time=object())

    assert len(opened_paths) == 2
    assert all(_SID_SHORT in p for p in opened_paths)
    assert result is not None
    assert result["session_start_sha"] == "cccccccccccccccccccccccccccccccccccccccc"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_floor_kwargs_short_id_collision_still_filtered_by_field_after_open(monkeypatch, tmp_path):
    """Two files share this session's `-{sid[:8]}` filename segment, but one
    of them actually belongs to a DIFFERENT full session id that happens to
    collide on the first 8 characters — the name filter alone cannot
    distinguish them (by design, it is a pre-filter only), so the exact
    `session_id`-field check after load must still exclude the impostor."""
    collider_full_sid = _SID_SHORT + "-ffff-ffff-ffff-ffffffffffff"
    assert collider_full_sid[:8] == _SID_SHORT
    assert collider_full_sid != _SID

    live_dir = tmp_path / "state" / "review-trail"
    _write_record(live_dir, f"2026-08-21-000001-{_SID_SHORT}.json", session_id=_SID, sha_range="aaaaaaa^..aaaaaaa")
    _write_record(
        live_dir, f"2026-08-21-000002-{_SID_SHORT}.json", session_id=collider_full_sid, sha_range="bbbbbbb^..bbbbbbb"
    )

    monkeypatch.setattr(wsc, "_resolve_session_start_sha", lambda root, t: "cccccccccccccccccccccccccccccccccccccccc")
    monkeypatch.setattr(wsc, "_resolve_head_sha", lambda root: "dddddddddddddddddddddddddddddddddddddddd")

    result = wsc._resolve_review_brightline_floor_kwargs(tmp_path, _SID, session_start_time=object())

    assert result is not None
    # Only the genuine own-session record's tip contributes — the collider's
    # `bbbbbbb` tip must never appear.
    tips = [r["sha_range_head"] for r in result["trail_records"]]
    assert tips == ["aaaaaaa"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_floor_kwargs_zero_own_records_among_decoys_returns_none(monkeypatch, tmp_path):
    """A large foreign-session corpus and zero records of this session's own
    — the ordinary AC2 single-close path — must still resolve `None` (the
    caller's byte-identical plain-call fallback), never a guessed floor."""
    live_dir = tmp_path / "state" / "review-trail"
    for i in range(50):
        _write_record(live_dir, _decoy_filename(i), session_id=_FOREIGN_SID)

    result = wsc._resolve_review_brightline_floor_kwargs(tmp_path, _SID, session_start_time=object())

    assert result is None
