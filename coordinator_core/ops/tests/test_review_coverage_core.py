"""
Tests for coordinator_core.ops.review_coverage_core.build_segments.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md § C3a

Pins the per-range memoisation fix on both the `git rev-list` leg and the
`git log --name-only` leg of `build_segments`: a repeated `sha_range`
across multiple trail records must resolve EACH of those two git calls
only ONCE per distinct range, while every record still gets its own
segment dict (memoised set emitted into each, per-segment file
attribution preserved).

Negative-spec: does not test multi-range batching (forbidden — see
build_segments's inline comment: SAFE_RANGE admits symbolic/live-HEAD
endpoints and git computes reachable(positives) \\ reachable(negatives) as
one set expression per range).
"""

from typing import List, Tuple

from coordinator_core.ops import review_coverage_core as rcc


def _rec(sha_range: str, artifact: str) -> Tuple[str, dict]:
    return (
        "trail.jsonl",
        {
            "sha_range": sha_range,
            "artifact": artifact,
            "verdict": "ok",
        },
    )


def test_build_segments_dedupes_repeated_range_revlist_calls(monkeypatch):
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            sha_range = cmd[2]
            return 0, f"sha-for-{sha_range}\n", ""
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            return 0, f"file-for-{sha_range}.py\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    all_records = [
        _rec("aaa..bbb", "record-1"),
        _rec("aaa..bbb", "record-2"),
        _rec("aaa..bbb", "record-3"),
        _rec("ccc..ddd", "record-4"),
    ]

    segments = rcc.build_segments(all_records, on_unresolvable_ref="fail")

    revlist_calls = [c for c in calls if c[:2] == ["git", "rev-list"]]
    log_calls = [c for c in calls if c[:2] == ["git", "log"]]

    # rev-list: one call per DISTINCT range (2 distinct ranges), not one per record (4).
    assert len(revlist_calls) == 2, revlist_calls

    # git log --name-only: also one call per DISTINCT range (2), not one
    # per record (4) — memoised the same way as rev-list.
    assert len(log_calls) == 2, log_calls

    # All 4 records still produce a segment (memoised shas reused correctly).
    assert len(segments) == 4
    for seg in segments:
        assert seg["shas"] == [f"sha-for-{seg['sha_range']}"]
        assert seg["files"] == [f"file-for-{seg['sha_range']}.py"]


def test_build_segments_skip_on_unresolvable_ref_is_memoised(monkeypatch):
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return 1, "", "fatal: bad range"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    all_records = [
        _rec("bad..range", "record-1"),
        _rec("bad..range", "record-2"),
    ]

    segments = rcc.build_segments(all_records, on_unresolvable_ref="skip")

    revlist_calls = [c for c in calls if c[:2] == ["git", "rev-list"]]
    assert len(revlist_calls) == 1, revlist_calls
    assert segments == []


def test_build_segments_dedupes_repeated_range_namelog_calls(monkeypatch):
    """Pins the `git log --name-only` leg's own memo (namelog_memo):
    a repeated sha_range must resolve `git log --name-only` only ONCE,
    even when `git rev-list` for that same range is fed from a caller
    that doesn't share build_segments's revlist_memo shape (kept separate
    here from the rev-list dedup test so a future revlist_memo-only
    regression can't hide a namelog_memo miss)."""
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            sha_range = cmd[2]
            return 0, f"sha-for-{sha_range}\n", ""
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            return 0, f"file-for-{sha_range}.py\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    all_records = [
        _rec("aaa..bbb", "record-1"),
        _rec("aaa..bbb", "record-2"),
        _rec("ccc..ddd", "record-3"),
    ]

    segments = rcc.build_segments(all_records, on_unresolvable_ref="fail")

    log_calls = [c for c in calls if c[:2] == ["git", "log"]]
    assert len(log_calls) == 2, log_calls  # one per DISTINCT range, not per record

    assert len(segments) == 3
    for seg in segments:
        assert seg["shas"] == [f"sha-for-{seg['sha_range']}"]
        assert seg["files"] == [f"file-for-{seg['sha_range']}.py"]


def test_build_segments_skip_on_unresolvable_namelog_is_memoised(monkeypatch):
    """git rev-list resolves fine, but git log --name-only fails for the
    range — with on_unresolvable_ref="skip" a repeated range must only
    spawn `git log` ONCE, skipping both records, not re-spawn per record."""
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return 0, "sha1\n", ""
        if cmd[:2] == ["git", "log"]:
            return 1, "", "fatal: bad range"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    all_records = [
        _rec("bad..range", "record-1"),
        _rec("bad..range", "record-2"),
    ]

    segments = rcc.build_segments(all_records, on_unresolvable_ref="skip")

    log_calls = [c for c in calls if c[:2] == ["git", "log"]]
    assert len(log_calls) == 1, log_calls
    assert segments == []
