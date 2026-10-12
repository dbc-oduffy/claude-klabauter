"""Pins the content-keyed parse memo in ``records_query._load_record``."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import records_query as rq


@pytest.fixture(autouse=True)
def _clear_caches():
    rq._parse_frontmatter_cached.cache_clear()
    rq._parse_yaml_cached.cache_clear()
    yield
    rq._parse_frontmatter_cached.cache_clear()
    rq._parse_yaml_cached.cache_clear()


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding='utf-8')


def test_same_size_edit_is_a_miss_and_returns_new_frontmatter(tmp_path):
    f = tmp_path / 'h.md'
    _write(f, '---\nkind: aaaa\n---\nbody\n')
    first = rq._load_record(f, tmp_path, 'handoff')
    assert first['frontmatter']['kind'] == 'aaaa'
    _write(f, '---\nkind: bbbb\n---\nbody\n')
    second = rq._load_record(f, tmp_path, 'handoff')
    assert second['frontmatter']['kind'] == 'bbbb'


def test_yaml_branch_same_size_edit_is_a_miss(tmp_path):
    f = tmp_path / 'd.yaml'
    _write(f, 'status: open\n')
    assert rq._load_record(f, tmp_path, 'debt')['frontmatter']['status'] == 'open'
    _write(f, 'status: done\n')
    assert rq._load_record(f, tmp_path, 'debt')['frontmatter']['status'] == 'done'


@pytest.mark.parametrize('name,rtype,text', [
    ('h.md', 'handoff', '---\nkind: x\ntags:\n  - a\n---\nbody\n'),
    ('d.yaml', 'debt', 'status: open\ntags:\n  - a\n'),
])
def test_mutating_a_returned_record_does_not_change_the_next_hit(tmp_path, name, rtype, text):
    f = tmp_path / name
    _write(f, text)
    first = rq._load_record(f, tmp_path, rtype)
    first['frontmatter']['tags'].append('mutated')
    first['frontmatter']['injected'] = True
    second = rq._load_record(f, tmp_path, rtype)
    assert second['frontmatter'].get('tags') == ['a']
    assert 'injected' not in second['frontmatter']
    info = (rq._parse_frontmatter_cached if name.endswith('.md') else rq._parse_yaml_cached).cache_info()
    assert info.hits == 1


def test_cache_is_bounded():
    for fn in (rq._parse_frontmatter_cached, rq._parse_yaml_cached):
        assert fn.cache_info().maxsize == rq._PARSE_CACHE_MAXSIZE
        assert isinstance(rq._PARSE_CACHE_MAXSIZE, int) and rq._PARSE_CACHE_MAXSIZE > 0
    for i in range(rq._PARSE_CACHE_MAXSIZE + 10):
        rq._parse_yaml_cached(f'k: {i}\n')
    assert rq._parse_yaml_cached.cache_info().currsize == rq._PARSE_CACHE_MAXSIZE


def test_persisted_record_cache_serves_unchanged_files_and_follows_edits(tmp_path):
    import os

    from coordinator_core.ops import records_query as rq
    from coordinator_core.ops.ceremony.records_query import query_records

    (tmp_path / ".git").mkdir()
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    rec = handoffs / "2026-01-01-a.md"
    rec.write_text("---\nstatus: active\ncreated: 2026-01-01\n---\nbody\n", encoding="utf-8")
    rq._RECORD_CACHES.clear()

    first = query_records("handoff", tmp_path)
    assert (tmp_path / ".git" / "claude-klabauter-records-query-cache-handoff.json").is_file()

    rq._RECORD_CACHES.clear()
    real = rq._load_record_uncached
    rq._load_record_uncached = lambda *a, **k: (_ for _ in ()).throw(AssertionError("reparsed"))
    try:
        assert query_records("handoff", tmp_path) == first
    finally:
        rq._load_record_uncached = real

    rec.write_text("---\nstatus: shipped\ncreated: 2026-01-01\n---\nbody!\n", encoding="utf-8")
    rq._RECORD_CACHES.clear()
    assert query_records("handoff", tmp_path)[0]["frontmatter"]["status"] == "shipped"

    (tmp_path / ".git" / "claude-klabauter-records-query-cache-handoff.json").write_text("x{", encoding="utf-8")
    rq._RECORD_CACHES.clear()
    assert query_records("handoff", tmp_path)[0]["frontmatter"]["status"] == "shipped"
