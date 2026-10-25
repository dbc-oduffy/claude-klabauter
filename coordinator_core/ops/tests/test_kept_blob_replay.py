"""history_bases and replay_exact: historical bases, commit-time cut-offs, exact raw match."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from coordinator_core.ops import kept_blob_replay as kr
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.kept_blob_transcripts import ToolEdit
from coordinator_core.ops.tests._kept_blob_fixture import commit_at, git_repo


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _edit(day, name, inp, n=0):
    return ToolEdit(_ts(day), f"t{day}-{n}", name, inp, "s/f.jsonl")


def _e(day, old, new):
    return _edit(day, "Edit", {"old_string": old, "new_string": new})


def test_hit_from_historical_blob_not_head_or_base(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", b"one\n", "2026-01-01T00:00:00Z")
    commit_at(repo, "a.txt", b"two\n", "2026-01-02T00:00:00Z")
    commit_at(repo, "a.txt", b"three\n", "2026-01-03T00:00:00Z")
    c, _ = commit_at(repo, "b.txt", b"x\n", "2026-01-04T00:00:00Z")
    bases = kr.history_bases(repo, ["a.txt"], base_sha=c)["a.txt"]
    kept = blob_sha(b"one!\n")
    hit = kr.replay_exact(bases, [_e(5, "one", "one!")], kept)
    assert hit is not None and hit.data == b"one!\n"
    assert hit.base.data == b"one\n" and hit.applied == 1


def test_commit_base_cutoff_vs_empty_base(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", b"hello\n", "2026-01-10T00:00:00Z")
    bases = kr.history_bases(repo, ["a.txt"])["a.txt"]
    early = _edit(5, "Write", {"content": "hello\n"})
    kept = blob_sha(b"hello world\n")
    edits = [early, _e(11, "hello", "hello world")]
    hit = kr.replay_exact(bases, edits, kept)
    # commit base would need the early Write (before its commit) to be applied: the empty base does.
    assert hit is not None and hit.base.label == "empty"
    only_commit = [b for b in bases if b.label != "empty"]
    assert kr.replay_exact(only_commit, [_e(5, "hello", "hello world")], kept) is None
    assert kr.replay_exact(only_commit, [_e(11, "hello", "hello world")], kept) is not None


def test_absent_old_string_skipped_and_replay_continues():
    bases = [kr.Base("empty", b"", None)]
    edits = [
        _edit(1, "Write", {"content": "abc\n"}),
        _e(2, "ZZZ", "y"),
        _e(3, "abc", "abd"),
    ]
    hit = kr.replay_exact(bases, edits, blob_sha(b"abd\n"))
    assert hit is not None and hit.applied == 2


def test_multiedit_whole_and_skipped_whole():
    base = [kr.Base("empty", b"a b\n", None)]
    ok = _edit(1, "MultiEdit", {"edits": [
        {"old_string": "a", "new_string": "x"}, {"old_string": "b", "new_string": "y"}]})
    assert kr.replay_exact(base, [ok], blob_sha(b"x y\n")) is not None
    bad = _edit(1, "MultiEdit", {"edits": [
        {"old_string": "a", "new_string": "x"}, {"old_string": "NOPE", "new_string": "y"}]})
    assert kr.apply_edit("a b\n", bad) is None
    assert kr.replay_exact(base, [bad], blob_sha(b"x b\n")) is None


def test_match_mid_sequence():
    bases = [kr.Base("empty", b"", None)]
    edits = [_edit(1, "Write", {"content": "a\n"}), _e(2, "a", "b"), _e(3, "b", "c")]
    hit = kr.replay_exact(bases, edits, blob_sha(b"b\n"))
    assert hit is not None and hit.applied == 2 and hit.data == b"b\n"


def test_near_miss_returns_none():
    bases = [kr.Base("empty", b"", None)]
    edits = [_edit(1, "Write", {"content": "abc\n"})]
    assert kr.replay_exact(bases, edits, blob_sha(b"abc")) is None


def test_base_equality_hit_applies_zero():
    b = kr.Base("empty", b"same\n", None)
    hit = kr.replay_exact([b], [], blob_sha(b"same\n"))
    assert hit is not None and hit.applied == 0


def test_history_bases_one_spawn_for_three_rels(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    for r in ("a.txt", "b.txt", "c.txt"):
        commit_at(repo, r, r.encode() + b"\n", "2026-01-01T00:00:00Z")
    calls = []
    real = subprocess.run

    def spy(*a, **k):
        calls.append(a[0])
        return real(*a, **k)

    monkeypatch.setattr(kr.subprocess, "run", spy)
    out = kr.history_bases(repo, ["a.txt", "b.txt", "c.txt"])
    assert len(calls) == 1
    assert all(len(v) == 2 for v in out.values())


def test_base_sha_uses_single_cat_file_spawn(tmp_path, monkeypatch):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", b"a\n", "2026-01-01T00:00:00Z")
    c, _ = commit_at(repo, "b.txt", b"b\n", "2026-01-02T00:00:00Z")
    calls = []
    real = subprocess.run
    monkeypatch.setattr(kr.subprocess, "run", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    out = kr.history_bases(repo, ["a.txt", "b.txt"], base_sha=c)
    assert len(calls) == 2
    assert any(b.label.startswith("base_sha:") for b in out["b.txt"])
    assert any(b.label.startswith("base_sha:") and b.since is not None for b in out["a.txt"])
