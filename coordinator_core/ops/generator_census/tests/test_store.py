"""Tests for the census blob-sha cache: round trip, corruption, header, writers, budget."""

from __future__ import annotations

import json
import os
import threading

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.ops.generator_census import store

declarations = pytest.importorskip("coordinator_core.ops.generator_census.declarations")

HEADER = {"extractor": "abc", "py": [3, 11], "machinery": "deadbeef"}


def _decls(tag: str = "x"):
    return declarations.Declarations(
        generates=(f"out/{tag}.json",),
        mutates=None,
        mutates_append=None,
        generates_external=None,
        unstamped_by_design=None,
    )


def _entries(n: int = 3):
    out = {f"{i:040x}": _decls(str(i)) for i in range(n)}
    out["f" * 40] = None
    return out


def test_round_trip(tmp_path):
    entries = _entries()
    store.save(tmp_path, HEADER, entries)
    assert store.load(tmp_path, HEADER) == entries


def test_negative_entries_survive(tmp_path):
    store.save(tmp_path, HEADER, {"a" * 40: None})
    loaded = store.load(tmp_path, HEADER)
    assert loaded == {"a" * 40: None}


def test_malformed_marker_survives(tmp_path):
    d = declarations.Declarations(
        generates=declarations.MALFORMED,
        mutates=None,
        mutates_append=None,
        generates_external=None,
        unstamped_by_design=None,
    )
    store.save(tmp_path, HEADER, {"b" * 40: d})
    assert store.load(tmp_path, HEADER)["b" * 40].generates is declarations.MALFORMED


def test_absent_reads_empty(tmp_path):
    assert store.load(tmp_path / "nope", HEADER) == {}


def test_truncated_blob_reads_empty(tmp_path):
    store.save(tmp_path, HEADER, _entries())
    path = tmp_path / store.CACHE_FILENAME
    path.write_bytes(path.read_bytes()[:-20])
    assert store.load(tmp_path, HEADER) == {}


def test_garbage_reads_empty(tmp_path):
    (tmp_path / store.CACHE_FILENAME).write_bytes(b"\xff\x00not json")
    assert store.load(tmp_path, HEADER) == {}


@pytest.mark.parametrize(
    "field,value", [("extractor", "zzz"), ("py", [3, 12]), ("machinery", "cafe")]
)
def test_each_header_field_mismatch_reads_empty(tmp_path, field, value):
    store.save(tmp_path, HEADER, _entries())
    assert store.load(tmp_path, {**HEADER, field: value}) == {}


def test_tuple_header_matches_after_json_round_trip(tmp_path):
    header = {**HEADER, "py": (3, 11)}
    store.save(tmp_path, header, _entries())
    assert store.load(tmp_path, header) != {}


def test_save_prunes_to_given_keys(tmp_path):
    store.save(tmp_path, HEADER, _entries(5))
    keep = {k: v for k, v in list(_entries(5).items())[:2]}
    store.save(tmp_path, HEADER, keep)
    assert set(store.load(tmp_path, HEADER)) == set(keep)


def test_interleaved_writers_leave_loadable_file(tmp_path):
    a = {f"{i:040x}": _decls("a") for i in range(200)}
    b = {f"{i:040x}": _decls("b") for i in range(100, 400)}
    threads = [
        threading.Thread(target=lambda e=e: [store.save(tmp_path, HEADER, e) for _ in range(5)])
        for e in (a, b)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.load(tmp_path, HEADER) in (a, b)
    assert [p.name for p in tmp_path.iterdir()] == [store.CACHE_FILENAME]


def test_unwritable_dir_is_noop_with_one_stderr_line(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store.save(blocker / "cache", HEADER, _entries())
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert store.load(blocker / "cache", HEADER) == {}


def test_unchanged_entries_skip_rewrite(tmp_path):
    store.save(tmp_path, HEADER, _entries())
    path = tmp_path / store.CACHE_FILENAME
    os.utime(path, (1, 1))
    store.save(tmp_path, HEADER, _entries())
    assert os.stat(path).st_mtime == 1


def test_extractor_digest_is_stable_hex():
    d = store.extractor_digest()
    assert d == store.extractor_digest()
    assert len(d) == 40 and int(d, 16) >= 0


def test_load_budget_6000_entries(tmp_path):
    entries = {f"{i:040x}": (_decls(str(i)) if i % 2 else None) for i in range(6000)}
    store.save(tmp_path, HEADER, entries)
    assert len(store.load(tmp_path, HEADER)) == 6000
    figure = in_process_time_ms(lambda: store.load(tmp_path, HEADER))
    assert figure["process_time_ms"] <= 20, figure


def test_file_is_plain_json(tmp_path):
    store.save(tmp_path, HEADER, _entries())
    blob = json.loads((tmp_path / store.CACHE_FILENAME).read_bytes())
    assert set(blob) == {"header", "entries"}
