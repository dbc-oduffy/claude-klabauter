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
    # Every chunk's timeout is the SAME flat bound regardless of how many
    # targets it carries -- the DR-349 fix. A per-item multiplier here
    # (`_DELETE_TIMEOUT_SECS * len(chunk)`) would make these two differ.
    assert calls[0][1] == float(cruft_sweep._DELETE_TIMEOUT_SECS)
    assert calls[1][1] == float(cruft_sweep._DELETE_TIMEOUT_SECS)
    assert calls[0][1] == calls[1][1]
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


# ---------------------------------------------------------------------------
# DR-349 § 4 -- the deletion phase derives its bound from the sweep's deadline
# ---------------------------------------------------------------------------


def test_chunk_timeout_is_clamped_to_the_watchdog_remainder(tmp_path, monkeypatch):
    """A supplied watchdog's REMAINDER caps each chunk's bound, so stacking
    chunks cannot buy more time than the phase's own ceiling grants."""
    a, b = (tmp_path / n for n in ("a.txt", "b.txt"))
    for p in (a, b):
        _touch(p)

    calls = []

    def _fake_run(argv, timeout=None, **kwargs):
        calls.append(timeout)
        for t in argv[2:]:
            Path(t).unlink(missing_ok=True)
        return _FakeCompleted(0)

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    wd = cruft_sweep._Watchdog(ceiling_secs=1.5)
    cruft_sweep._delete_paths_batch([a, b], watchdog=wd)

    assert len(calls) == 1
    assert 0.0 < calls[0] <= 1.5
    assert calls[0] < cruft_sweep._DELETE_TIMEOUT_SECS


def test_exhausted_watchdog_skips_the_spawn_but_still_reports_every_target(
    tmp_path, monkeypatch
):
    """Past the deadline no `rm -rf` is spawned at all -- the phase stops
    occupying the box -- and every target still gets a verdict from its own
    post-hoc `exists()` check, so the return shape is unchanged."""
    a, b = (tmp_path / n for n in ("a.txt", "b.txt"))
    _touch(a)
    _touch(b)
    b.unlink()

    def _fake_run(argv, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("spawned rm -rf past the watchdog deadline")

    monkeypatch.setattr(cruft_sweep.subprocess, "run", _fake_run)

    wd = cruft_sweep._Watchdog(ceiling_secs=0.0)
    results = cruft_sweep._delete_paths_batch([a, b], watchdog=wd)

    assert results == {str(a): False, str(b): True}
    assert a.exists()


def test_watchdog_env_knob_may_lower_the_ceiling_but_never_raise_it(monkeypatch):
    """DR-349 § 3: `CRUFT_SWEEP_WATCHDOG_CEILING_SECS` is narrow-only. A stale
    `export` in a sibling repo must not be able to grant this sweep more of a
    box already carrying 50-70 concurrent sessions."""
    default = float(cruft_sweep._WATCHDOG_CEILING_SECS_DEFAULT)

    monkeypatch.setenv("CRUFT_SWEEP_WATCHDOG_CEILING_SECS", "5")
    assert cruft_sweep._resolve_watchdog_ceiling_secs() == 5.0

    monkeypatch.setenv("CRUFT_SWEEP_WATCHDOG_CEILING_SECS", str(default * 10))
    assert cruft_sweep._resolve_watchdog_ceiling_secs() == default

    monkeypatch.setenv("CRUFT_SWEEP_WATCHDOG_CEILING_SECS", "not-a-number")
    assert cruft_sweep._resolve_watchdog_ceiling_secs() == default

    monkeypatch.delenv("CRUFT_SWEEP_WATCHDOG_CEILING_SECS")
    assert cruft_sweep._resolve_watchdog_ceiling_secs() == default


def test_watchdog_remaining_is_floored_at_zero():
    """`remaining()` is handed straight to `min()`; a negative remainder would
    silently invert the clamp into a widening."""
    assert cruft_sweep._Watchdog(ceiling_secs=-10.0).remaining() == 0.0
    assert cruft_sweep._Watchdog(ceiling_secs=30.0).remaining() > 0.0
