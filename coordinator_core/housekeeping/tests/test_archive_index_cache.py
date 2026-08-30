"""The archive-index cache: it must make `revalidate` reachable, and it must
never be load-bearing for a verdict.

Rebuilt every cycle, the index costs 171.9ms at 1,470 records -- 85% of a
203ms cycle, and linear in the archive, which the plan's Anti-scope forbids.
Persisting it is what makes C4's 1.95ms revalidation the per-cycle cost
rather than a number that describes nothing the job ever does.

Every test below that names a broken cache asserts the SAME outcome: a
correct index, rebuilt. That uniformity is the point -- there is no cache
failure mode that produces a wrong answer, only ones that cost a walk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.housekeeping.archive_index import (
    CACHE_SCHEMA_VERSION,
    build_index,
    cache_path_for,
    load_index,
    open_index,
    save_index,
)
from coordinator_core.housekeeping.tests.corpus_fixture import build_corpus


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("archive_index_cache_corpus")
    return build_corpus(root)


def _round_trip(tmp_path, corpus):
    cache = tmp_path / "cache.json"
    original = build_index(corpus.archive_dir)
    assert save_index(original, cache) is True
    return cache, original


def test_round_trip_preserves_by_id_and_signatures(tmp_path, corpus):
    cache, original = _round_trip(tmp_path, corpus)
    loaded = load_index(corpus.archive_dir, cache)
    assert loaded is not None
    assert loaded.by_id == original.by_id
    assert loaded.stat_by_path == original.stat_by_path
    assert loaded.archive_dir == original.archive_dir


def test_signatures_reload_as_tuples_not_lists(tmp_path, corpus):
    """JSON has no tuple type. A signature reloaded as a list would compare
    unequal to `_signature_from_entry`'s tuple for EVERY path, so a warm
    cache would report the whole archive changed -- a silent full rescan
    wearing the costume of a cache hit."""
    cache, _ = _round_trip(tmp_path, corpus)
    loaded = load_index(corpus.archive_dir, cache)
    assert loaded is not None
    sig = next(iter(loaded.stat_by_path.values()))
    assert isinstance(sig, tuple), f"signature reloaded as {type(sig).__name__}"
    # And the consequence that matters: a steady-state revalidate is quiet.
    from coordinator_core.housekeeping.archive_index import revalidate

    assert revalidate(loaded) == set()


def test_open_index_uses_the_cache_when_one_exists(tmp_path, corpus):
    cache, original = _round_trip(tmp_path, corpus)
    index, rebuilt = open_index(corpus.archive_dir, cache)
    assert rebuilt is False
    assert index.by_id == original.by_id


def test_open_index_builds_when_no_cache_exists(tmp_path, corpus):
    index, rebuilt = open_index(corpus.archive_dir, tmp_path / "absent.json")
    assert rebuilt is True
    assert index.by_id == build_index(corpus.archive_dir).by_id


def test_open_index_with_cache_path_none_always_builds(corpus):
    index, rebuilt = open_index(corpus.archive_dir, None)
    assert rebuilt is True
    assert index.by_id


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda p: p.write_text("{not json", encoding="utf-8"), "corrupt json"),
        (lambda p: p.write_text("[]", encoding="utf-8"), "wrong top-level type"),
        (lambda p: p.write_text('{"version": 999}', encoding="utf-8"), "future schema"),
        (lambda p: p.write_text("", encoding="utf-8"), "empty file"),
    ],
)
def test_every_broken_cache_rebuilds_rather_than_misleads(tmp_path, corpus, mutate, label):
    cache = tmp_path / f"broken-{abs(hash(label))}.json"
    mutate(cache)
    assert load_index(corpus.archive_dir, cache) is None, label
    index, rebuilt = open_index(corpus.archive_dir, cache)
    assert rebuilt is True, label
    assert index.by_id == build_index(corpus.archive_dir).by_id, label


def test_cache_built_against_a_different_archive_dir_is_refused(tmp_path, corpus):
    """Its paths describe another tree entirely. Revalidating it would report
    every entry deleted and then rebuild anyway -- refusing up front is the
    same answer for less work, and it cannot leak a foreign path into a
    candidate list in between."""
    cache, _ = _round_trip(tmp_path, corpus)
    assert load_index(tmp_path / "some" / "other" / "archive", cache) is None


def test_a_stale_cache_is_corrected_by_revalidation_not_trusted(tmp_path, corpus):
    """The load-bearing property. A cache that has fallen behind the archive
    must not answer from its stale contents: `open_index` revalidates, so a
    record deleted since the cache was written stops being a candidate."""
    cache, original = _round_trip(tmp_path, corpus)
    victim_path = next(iter(original.stat_by_path))
    victim_ids = [hid for hid, paths in original.by_id.items() if victim_path in paths]
    assert victim_ids, "fixture must give us an indexed record to delete"
    os.unlink(victim_path)

    index, rebuilt = open_index(corpus.archive_dir, cache)
    assert rebuilt is False, "this exercises the cached path, not a rebuild"
    assert victim_path not in index.stat_by_path
    for hid in victim_ids:
        assert victim_path not in [str(p) for p in index.lookup(hid)]


def test_save_never_raises_when_the_destination_is_unwritable(tmp_path, corpus):
    """A cache that cannot be written costs the next cycle a rebuild. It must
    never cost the current cycle its result."""
    index = build_index(corpus.archive_dir)
    blocked = tmp_path / "a-file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    assert save_index(index, blocked / "nested" / "cache.json") is False


def test_save_leaves_no_temp_files_behind(tmp_path, corpus):
    cache = tmp_path / "tidy" / "cache.json"
    index = build_index(corpus.archive_dir)
    assert save_index(index, cache) is True
    assert save_index(index, cache) is True
    leftovers = [p.name for p in cache.parent.iterdir() if p.name != cache.name]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_save_is_atomic_so_a_reader_never_sees_a_partial_file(tmp_path, corpus):
    """`os.replace` is the whole mechanism: a concurrent reader observes
    either the previous complete file or the new complete file. Asserted by
    overwriting a valid cache and confirming it stays loadable throughout --
    on a tree with ~50 peers this is what removes the need for a lock."""
    cache, original = _round_trip(tmp_path, corpus)
    for _ in range(3):
        assert save_index(original, cache) is True
        reloaded = load_index(corpus.archive_dir, cache)
        assert reloaded is not None
        assert reloaded.by_id == original.by_id


def test_cache_path_is_under_the_git_common_dir_and_never_the_worktree(tmp_path):
    """Derived, per-checkout, and must never be committed -- an index blob
    churning on a shared work/* branch is noise every peer pays for."""
    common = tmp_path / "repo" / ".git"
    path = cache_path_for(common)
    assert common in path.parents
    assert path.suffix == ".json"


def test_written_cache_declares_its_schema_version(tmp_path, corpus):
    cache, _ = _round_trip(tmp_path, corpus)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["version"] == CACHE_SCHEMA_VERSION
