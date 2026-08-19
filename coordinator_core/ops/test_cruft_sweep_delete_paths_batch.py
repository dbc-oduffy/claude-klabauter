"""Direct multi-item coverage for `_delete_paths_batch`
(`coordinator_core.ops.cruft_sweep`), added per review finding amp-s1 #5
(WARN: none of the four new batched helpers from
docs/plans/2026-08-19-burn-down-the-amplification-hitlist.md C5 had direct
tests). `_delete_paths_batch` already special-cases `len(targets) == 1` by
delegating to `_delete_path`, so a single-target test is a no-op through
this function -- every test below uses >=2 targets to actually exercise the
batch/chunking path, and the chunking test specifically pins the amp-s1 #1
fix (fixed-size chunks instead of one `60s * N` unbounded call), which
would fail against the pre-fix single-call shape: that shape issued exactly
one `subprocess.run`, never `_DELETE_BATCH_CHUNK_SIZE + 1` targets split
across two calls.

`subprocess.run` is mocked throughout -- no real `rm -rf` spawn, so this
file needs neither `spawns_process` nor `cadence` (see
`coordinator_core/tests/test_no_new_spawning_tests.py`).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import cruft_sweep


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


class _FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_multi_target_batch_deletes_only_files_the_fake_rm_actually_touches(tmp_path, monkeypatch):
    """Outcome is read off `exists()` per target, never off the subprocess
    call's own success -- mirrors `_delete_path`'s contract, applied to a
    batch of >1. Only `a` and `c` are pre-deleted here to prove each
    target's own post-hoc existence check drives its own verdict."""
    a, b, c = (tmp_path / n for n in ("a.txt", "b.txt", "c.txt"))
    for p in (a, b, c):
        _touch(p)

    def _fake_run(argv, **kwargs):
        # Simulate `rm -rf` really only having removed a and c.
        a.unlink(missing_ok=True)
        c.unlink(missing_ok=True)
        return _FakeCompleted(0)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    results = cruft_sweep._delete_paths_batch([a, b, c])

    assert results == {str(a): True, str(b): False, str(c): True}
    assert b.exists()


def test_large_batch_is_split_into_fixed_size_chunks(tmp_path, monkeypatch):
    """Regression pin for review finding amp-s1 #1: a batch bigger than
    `_DELETE_BATCH_CHUNK_SIZE` must be issued as multiple bounded
    subprocess calls, not one call timed out at `60s * N`. Before the fix
    this drove exactly one `subprocess.run` call for the whole batch --
    this test fails against that shape because it asserts more than one."""
    n = cruft_sweep._DELETE_BATCH_CHUNK_SIZE + 3
    targets = [tmp_path / f"t{i}.txt" for i in range(n)]
    for p in targets:
        _touch(p)

    calls = []

    def _fake_run(argv, timeout=None, **kwargs):
        calls.append((len(argv) - 2, timeout))  # argv[0:2] == ["rm", "-rf"]
        for a in argv[2:]:
            Path(a).unlink(missing_ok=True)
        return _FakeCompleted(0)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    results = cruft_sweep._delete_paths_batch(targets)

    assert len(calls) == 2
    assert calls[0][0] == cruft_sweep._DELETE_BATCH_CHUNK_SIZE
    assert calls[1][0] == 3
    # Every chunk's own timeout stays bounded by ITS OWN size, not the
    # whole batch's size -- the fixed ceiling the fix exists to give.
    assert calls[0][1] == cruft_sweep._DELETE_TIMEOUT_SECS * cruft_sweep._DELETE_BATCH_CHUNK_SIZE
    assert calls[1][1] == cruft_sweep._DELETE_TIMEOUT_SECS * 3
    assert all(results.values())
    assert not any(p.exists() for p in targets)


def test_one_chunk_timing_out_does_not_stop_later_chunks(tmp_path, monkeypatch):
    """A stuck/timed-out chunk must not block chunks after it -- the
    liveness property amp-s1 #1 asked for. The first chunk's targets stay
    on disk (undeleted, so `False`); the second chunk still runs and
    succeeds."""
    n = cruft_sweep._DELETE_BATCH_CHUNK_SIZE + 2
    targets = [tmp_path / f"t{i}.txt" for i in range(n)]
    for p in targets:
        _touch(p)

    call_n = {"i": 0}

    def _fake_run(argv, timeout=None, **kwargs):
        call_n["i"] += 1
        if call_n["i"] == 1:
            raise cruft_sweep.subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        for a in argv[2:]:
            Path(a).unlink(missing_ok=True)
        return _FakeCompleted(0)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    results = cruft_sweep._delete_paths_batch(targets)

    first_chunk = targets[: cruft_sweep._DELETE_BATCH_CHUNK_SIZE]
    second_chunk = targets[cruft_sweep._DELETE_BATCH_CHUNK_SIZE :]
    assert all(results[str(p)] is False for p in first_chunk)
    assert all(results[str(p)] is True for p in second_chunk)
    assert all(p.exists() for p in first_chunk)
    assert not any(p.exists() for p in second_chunk)
