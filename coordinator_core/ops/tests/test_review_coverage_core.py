"""
Tests for coordinator_core.ops.review_coverage_core.build_segments.

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § C3a

Pins the per-range memoisation fix on `build_segments`' single combined
`git log --format=%H --name-only` call: a repeated `sha_range` across
multiple trail records must resolve that call only ONCE per distinct
range, while every record still gets its own segment dict (memoised set
emitted into each, per-segment file attribution preserved).

The two legs these tests originally pinned (`git rev-list` for the sha set
and `git log --name-only` for the file set) were merged into that one call
by C3a's successor, pln-composition-invocation-budgets § C17: one range
walk answers both questions, halving the spawn count per distinct range.
The memo contract these tests exist to pin is unchanged — only the number
of calls being memoised went from two to one.

Line-shape note: the fakes below must emit 40-char lowercase-hex lines for
anything meant to parse as a SHA. `_parse_combined_log_output` disambiguates
the interleaved output by that shape alone, so a placeholder like
`sha-for-<range>` parses as a FILENAME, not a sha.

Negative-spec: does not test multi-range batching (forbidden — see
build_segments's inline comment: SAFE_RANGE admits symbolic/live-HEAD
endpoints and git computes reachable(positives) \\ reachable(negatives) as
one set expression per range).
"""

import hashlib
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


def _sha_for(sha_range: str) -> str:
    """Deterministic 40-char lowercase-hex sha, distinct per range, so the
    combined-log fakes below emit something `_parse_combined_log_output`
    actually recognises as a SHA line (not a placeholder that parses as a
    filename)."""
    return hashlib.sha1(sha_range.encode()).hexdigest()


def test_build_segments_dedupes_repeated_range_revlist_calls(monkeypatch):
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            return 0, f"{_sha_for(sha_range)}\nfile-for-{sha_range}.py\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    all_records = [
        _rec("aaa..bbb", "record-1"),
        _rec("aaa..bbb", "record-2"),
        _rec("aaa..bbb", "record-3"),
        _rec("ccc..ddd", "record-4"),
    ]

    segments = rcc.build_segments(all_records, on_unresolvable_ref="fail")

    log_calls = [c for c in calls if c[:2] == ["git", "log"]]

    # The combined `git log --format=%H --name-only` call: one per DISTINCT
    # range (2 distinct ranges), not one per record (4).
    assert len(log_calls) == 2, log_calls

    # All 4 records still produce a segment (memoised shas/files reused correctly).
    assert len(segments) == 4
    for seg in segments:
        assert seg["shas"] == [_sha_for(seg["sha_range"])]
        assert seg["files"] == [f"file-for-{seg['sha_range']}.py"]


def test_build_segments_skip_on_unresolvable_ref_is_memoised(monkeypatch):
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
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


def test_build_segments_dedupes_repeated_range_namelog_calls(monkeypatch):
    """Pins the combined `git log --format=%H --name-only` spawn's memo
    (segment_memo): a repeated sha_range must resolve that call only ONCE,
    with per-record segment dicts and per-segment file attribution still
    preserved (kept as its own test, separate from the sibling dedup test
    above, so a future segment_memo regression that only shows up under a
    different record/range shape can't hide behind the other test passing)."""
    calls: List[List[str]] = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            return 0, f"{_sha_for(sha_range)}\nfile-for-{sha_range}.py\n", ""
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
        assert seg["shas"] == [_sha_for(seg["sha_range"])]
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
