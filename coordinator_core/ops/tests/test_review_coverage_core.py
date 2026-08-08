"""
Tests for coordinator_core.ops.review_coverage_core.build_segments.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md § C3a

Pins the per-range memoisation fix on the `git rev-list` leg of
`build_segments`: a repeated `sha_range` across multiple trail records must
resolve `git rev-list` only ONCE, while the sibling `git log --name-only`
leg (per-segment file attribution) must still run once PER record, even
when `sha_range` repeats — that leg is deliberately not deduped.

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

    # git log --name-only: one call PER RECORD (4) — per-segment file
    # attribution is not deduped, unlike rev-list.
    assert len(log_calls) == 4, log_calls

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
