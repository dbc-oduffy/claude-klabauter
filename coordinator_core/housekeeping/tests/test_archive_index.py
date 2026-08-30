"""
Tests for coordinator_core.housekeeping.archive_index — the id -> [path]
archive candidate index, revalidated by scandir (plan chunk C4).

Covers: build_index's basic id -> path mapping (including the archive's
both-shapes layout, nested and root-level), revalidate's add/modify/delete
detection (including the in-place-modify-with-unchanged-size case the spike
verdict's own probe trap calls out), and the 5ms independent leg budget on
the ~1,470-record corpus fixture (C1).

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
  § C4.

Negative-spec: this file does NOT test C5's resolver (the act-time re-read
that turns a candidate into a verdict) — only the index's own build/lookup/
revalidate mechanics.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.housekeeping.archive_index import (
    build_index,
    revalidate,
)
from coordinator_core.housekeeping.tests.corpus_fixture import build_corpus


def _write_record(path: Path, blocker_id: str, body: str = "body\n") -> None:
    """`blocker_id` is written as `stub_id` -- what the index keys on, and
    what a gate's `blocked_by` actually names. Deliberately NOT `handoff_id`:
    an index keyed by handoff_id is one no blocker lookup can ever hit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" disables Windows' \n -> \r\n write-time translation, so a
    # byte-length comparison between two writes of same-length text (the
    # unchanged-size in-place-modify test below) is not corrupted by CRLF
    # expansion.
    path.write_text(
        f"---\nhandoff_id: hnd-for-{blocker_id}\nstub_id: {blocker_id}"
        f"\ndeployment_state: closed\n---\n{body}",
        encoding="utf-8",
        newline="",
    )


# ---------------------------------------------------------------------------
# build_index — basic mechanics
# ---------------------------------------------------------------------------


def test_build_index_maps_id_to_path(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "2026-01" / "rec-0001.md"
    _write_record(p, "hnd-abc")

    index = build_index(archive_dir)
    assert index.lookup("hnd-abc") == [p]
    assert index.stat_by_path[str(p)] == (
        p.stat().st_mtime_ns,
        p.stat().st_size,
    )


def test_build_index_covers_both_nested_and_root_level(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    nested = archive_dir / "2026-02" / "nested.md"
    root_level = archive_dir / "root.md"
    _write_record(nested, "hnd-nested")
    _write_record(root_level, "hnd-root")

    index = build_index(archive_dir)
    assert index.lookup("hnd-nested") == [nested]
    assert index.lookup("hnd-root") == [root_level]


def test_build_index_missing_id_field_is_omitted_not_an_error(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "no-id.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ndeployment_state: closed\n---\nbody\n", encoding="utf-8")

    index = build_index(archive_dir)
    assert index.lookup("anything") == []
    # the file is still real and stat-tracked, even though it has no id
    assert str(p) in index.stat_by_path


def test_lookup_miss_returns_empty_list(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    index = build_index(archive_dir)
    assert index.lookup("does-not-exist") == []


def test_build_index_onerror_receives_permission_errors(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    seen_errors = []

    def onerror(err: OSError) -> None:
        seen_errors.append(err)

    # Simulate a scan gap by pointing a sub-scandir at a nonexistent dir via
    # a broken symlink-free approach: scandir a path that gets removed
    # mid-flight is hard to force portably, so this asserts the onerror
    # plumbing directly by invoking scandir on a missing directory through
    # build_index's own internal walk entrypoint semantics -- the archive
    # root itself does not exist.
    missing_root = tmp_path / "does-not-exist"
    index = build_index(missing_root, onerror=onerror)
    assert index.by_id == {}
    assert len(seen_errors) == 1
    assert isinstance(seen_errors[0], OSError)


# ---------------------------------------------------------------------------
# revalidate — add / modify / delete detection
# ---------------------------------------------------------------------------


def test_revalidate_detects_a_new_file(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    existing = archive_dir / "existing.md"
    _write_record(existing, "hnd-existing")
    index = build_index(archive_dir)

    added = archive_dir / "added.md"
    _write_record(added, "hnd-added")

    changed = revalidate(index)
    assert added in changed
    assert index.lookup("hnd-added") == [added]


def test_revalidate_detects_a_deleted_file(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "gone.md"
    _write_record(p, "hnd-gone")
    index = build_index(archive_dir)
    assert index.lookup("hnd-gone") == [p]

    p.unlink()

    changed = revalidate(index)
    assert p in changed
    assert index.lookup("hnd-gone") == []
    assert str(p) not in index.stat_by_path


def test_revalidate_detects_true_in_place_modify(tmp_path):
    """Existing file, same name, same directory, content changed -- the
    probe trap the spike verdict names: assert the target exists BEFORE
    modifying it, or open(..., 'a') on a path the fixture never created
    would measure an ADD (which directory-mtime also catches), producing a
    false pass for the in-place-modify claim this test exists to check."""
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "modme.md"
    _write_record(p, "hnd-before", body="original body\n")
    assert p.exists(), "probe trap: target must exist before the in-place modify"

    index = build_index(archive_dir)
    before_sig = index.stat_by_path[str(p)]

    # Force a distinguishable mtime regardless of filesystem timestamp
    # resolution -- the correctness claim under test is signature
    # comparison, not wall-clock timing.
    new_mtime = time.time() + 5
    _write_record(p, "hnd-after", body="original body\n")
    os.utime(p, (new_mtime, new_mtime))

    changed = revalidate(index)
    assert p in changed
    assert index.stat_by_path[str(p)] != before_sig
    assert index.lookup("hnd-before") == []
    assert index.lookup("hnd-after") == [p]


def test_revalidate_detects_in_place_modify_with_unchanged_size(tmp_path):
    """The spike's own discriminating case: mtime is the ONLY signal when
    the modified content happens to be exactly the same byte length."""
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "samesize.md"
    _write_record(p, "hnd-aaaa")
    assert p.exists(), "probe trap: target must exist before the in-place modify"

    index = build_index(archive_dir)
    before_sig = index.stat_by_path[str(p)]
    before_size = p.stat().st_size

    # "hnd-aaaa" -> "hnd-bbbb": same length, different content.
    text = p.read_text(encoding="utf-8", newline="").replace("hnd-aaaa", "hnd-bbbb")
    assert len(text.encode("utf-8")) == len(p.read_bytes()), (
        "test setup drifted -- replacement must not change byte length"
    )
    new_mtime = time.time() + 5
    p.write_text(text, encoding="utf-8", newline="")
    os.utime(p, (new_mtime, new_mtime))

    assert p.stat().st_size == before_size, "test setup drifted -- size must be unchanged"

    changed = revalidate(index)
    assert p in changed, "an in-place modify with unchanged size must still be detected via mtime"
    assert index.stat_by_path[str(p)] != before_sig
    assert index.lookup("hnd-aaaa") == []
    assert index.lookup("hnd-bbbb") == [p]


def test_revalidate_reports_no_change_when_nothing_changed(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "stable.md"
    _write_record(p, "hnd-stable")
    index = build_index(archive_dir)

    changed = revalidate(index)
    assert changed == set()
    assert index.lookup("hnd-stable") == [p]


def test_revalidate_id_change_removes_stale_empty_id_entry(tmp_path):
    archive_dir = tmp_path / "archive" / "handoffs"
    p = archive_dir / "rename-id.md"
    _write_record(p, "hnd-old-id")
    index = build_index(archive_dir)
    assert "hnd-old-id" in index.by_id

    new_mtime = time.time() + 5
    _write_record(p, "hnd-new-id")
    os.utime(p, (new_mtime, new_mtime))
    revalidate(index)

    assert "hnd-old-id" not in index.by_id
    assert index.lookup("hnd-new-id") == [p]


# ---------------------------------------------------------------------------
# Leg budget — 5ms independent, at the real corpus's ~1,470-archived-record
# scale (C1's corpus fixture).
# ---------------------------------------------------------------------------

_REVALIDATE_BUDGET_MS = 5.0
_N_OUTER = 5
_K_INNER = 20
"""Windows' `time.process_time()` tick-quantises at ~15.6ms (this repo's own
documented reason for k-batching, e.g. test_archival_commit_process_budget.py's
K_INNER). A single un-batched revalidate() call is fast enough (1.95ms per the
spike verdict) to read as a full quantised tick or two by pure rounding noise,
which would fail a real, in-budget implementation -- batching K_INNER calls
inside one process_time() bracket and dividing amortises the tick away."""


_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
"""coordinator_core/housekeeping/tests/<this file> -> parents[3] is the repo
root -- same derivation test_archival_commit_process_budget.py's own
_CLAUDE_KLABAUTER_ROOT uses."""


@pytest.fixture(scope="module")
def scaled_index(tmp_path_factory):
    """Builds the corpus fixture UNDER THE REPO'S OWN DRIVE ANCHOR
    (`_CLAUDE_KLABAUTER_ROOT.anchor`), never pytest's default `tmp_path`/
    `tmp_path_factory` root -- this is load-bearing, not cosmetic.
    `tmp_path_factory`'s default root sits under the system TEMP directory,
    which on this box's own measurement is a DIFFERENT NTFS volume from the
    repo's own drive with 8dot3 (short-filename) generation enabled; the
    real archive directory this leg measures lives on the repo's own
    volume. Isolated confirmation (this chunk's own investigation, not
    inherited): identical fixture content/shape, same filename length
    (`YYYY-MM_NNNNNN_fixture-archived-NNNNN.md`, matching corpus_fixture.py's
    own real-shape convention), measured names-only (no stat) enumeration at
    ~16-18ms/call under the system TEMP volume vs ~1.6-2.3ms/call under this
    repo's own drive anchor -- an 8-10x gap explained entirely by filename
    length (short 8-char names read fast on BOTH volumes) rather than file
    count, directory count, or content size. Matches
    test_archival_commit_process_budget.py's own `_CLAUDE_KLABAUTER_ROOT.anchor`-
    rooted scratch-dir convention for exactly this reason -- a benchmark
    fixture must be measured on the volume that carries the real cost, not
    whichever one pytest defaults to.
    """
    dest_root = Path(_CLAUDE_KLABAUTER_ROOT.anchor) / f"_c4bench_{os.getpid()}"
    dest_root.mkdir(parents=True, exist_ok=True)
    root = dest_root / "corpus"
    fixture = build_corpus(root)
    index = build_index(fixture.archive_dir)
    yield fixture, index
    import shutil

    shutil.rmtree(dest_root, ignore_errors=True)


def test_revalidate_leg_budget_on_full_scale_corpus(scaled_index):
    """Leg budget, asserted independently (chunk C4 body): 5ms, tighter than
    the target-shape doc's 20ms row -- the spike measured 1.95ms at 1,470
    files, so 5ms is still 2.5x headroom. Median CPU (time.process_time)
    over N_REPS reps of a steady-state (no-change) revalidate pass, matching
    this repo's own convention for Windows tick-quantised timing
    (test_head_scan.py-adjacent modules; test_archival_commit_process_
    budget.py's own k-batching rationale) applied at module scope, not job
    -object scope, since this leg is pure Python with zero subprocesses."""
    _fixture, index = scaled_index
    assert len(index.stat_by_path) == pytest.approx(1470, rel=0.05), (
        "budget must be measured at the real corpus's ~1,470-archived-record "
        f"scale, not a toy fixture -- got {len(index.stat_by_path)}"
    )

    samples_ms = []
    for _ in range(_N_OUTER):
        start = time.process_time()
        for _inner in range(_K_INNER):
            changed = revalidate(index)
            assert changed == set(), "steady-state revalidate must report zero changes"
        elapsed_ms = (time.process_time() - start) * 1000.0 / _K_INNER
        samples_ms.append(elapsed_ms)

    samples_ms.sort()
    median_ms = samples_ms[len(samples_ms) // 2]
    assert median_ms <= _REVALIDATE_BUDGET_MS, (
        f"revalidate leg median {median_ms}ms exceeded the independent 5ms budget "
        f"at {len(index.stat_by_path)} archived files (samples: {samples_ms}ms) -- "
        "this is a real regression per this chunk's own body, not a tight budget"
    )
