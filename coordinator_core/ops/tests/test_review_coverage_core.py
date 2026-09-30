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


_AGGREGATE_TOKEN = "coverage assessment is partial"


def _aggregate_lines(err: str) -> List[str]:
    return [ln for ln in err.splitlines() if _AGGREGATE_TOKEN in ln]


def test_load_records_skip_emits_one_unparseable_file_aggregate(tmp_path, capsys):
    bad = tmp_path / "2026-01-01-bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    recs = rcc._load_records([str(bad)], "", "", "skip")

    assert recs == []
    lines = _aggregate_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "1 trail file(s)" in lines[0]


def test_build_segments_skip_emits_one_unresolvable_range_aggregate(monkeypatch, capsys):
    def fake_run(cmd, cwd=None):
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            if sha_range.startswith("bad"):
                return 1, "", "fatal: bad range"
            return 0, f"{_sha_for(sha_range)}\nfile-for-{sha_range}.py\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    records = [
        _rec("bad1..x", "record-1"),
        _rec("bad1..x", "record-2"),
        _rec("bad2..y", "record-3"),
        _rec("aaa..bbb", "record-4"),
    ]

    segments = rcc.build_segments(records, on_unresolvable_ref="skip")

    lines = _aggregate_lines(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "3 trail record(s)" in lines[0]
    assert "2 unresolvable range(s)" in lines[0]

    resolvable_only =rcc.build_segments([_rec("aaa..bbb", "record-4")], on_unresolvable_ref="skip")
    assert segments == resolvable_only
    assert len(segments) == 1


def test_build_segments_all_resolvable_emits_no_aggregate(monkeypatch, capsys):
    def fake_run(cmd, cwd=None):
        if cmd[:2] == ["git", "log"]:
            sha_range = cmd[-1]
            return 0, f"{_sha_for(sha_range)}\nfile-for-{sha_range}.py\n", ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(rcc, "_run", fake_run)

    rcc.build_segments(
        [_rec("aaa..bbb", "record-1"), _rec("ccc..ddd", "record-2")],
        on_unresolvable_ref="skip",
    )

    assert _aggregate_lines(capsys.readouterr().err) == []
