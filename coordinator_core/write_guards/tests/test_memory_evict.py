"""Tests for coordinator_core.write_guards._memory_evict."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.write_guards._memory_evict import evict_oldest


def _seed(mem, names_types):
    mem.mkdir()
    rows = []
    for i, (name, typ) in enumerate(names_types):
        p = mem / name
        fm = f"---\nname: {name}\ntype: {typ}\n---\nbody\n" if typ else "no frontmatter\n"
        p.write_text(fm, encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
        rows.append(f"- [{name}]({name}) - hook\n")
    (mem / "MEMORY.md").write_text("# idx\n" + "".join(rows), encoding="utf-8")


@pytest.mark.parametrize("typ", ["user", "feedback", "project", "reference"])
def test_evicts_oldest_row_and_logs(tmp_path, typ):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", typ), ("b.md", "user"), ("c.md", "user")])
    rec = evict_oldest(mem)
    assert rec["name"] == "a.md" and rec["type"] == typ
    assert not (mem / "a.md").exists() and (mem / "b.md").exists()
    idx = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert "a.md" not in idx and "b.md" in idx and "# idx" in idx
    lines = (tmp_path / "memory-evictions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert set(logged) == {"path", "name", "type", "evicted_at"}
    assert logged == rec
    assert logged["path"].endswith("a.md")


def test_unknown_type_without_frontmatter(tmp_path):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", None), ("b.md", "user")])
    assert evict_oldest(mem)["type"] == "unknown"


def test_empty_dir_returns_none_and_logs_nothing(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("# idx\n", encoding="utf-8")
    assert evict_oldest(mem) is None
    assert not (tmp_path / "memory-evictions.jsonl").exists()


def test_index_row_match_is_exact_link_target(tmp_path):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", "user"), ("xa.md", "user")])
    evict_oldest(mem)
    idx = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert "(xa.md)" in idx and "(a.md)" not in idx


def test_log_appends(tmp_path):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", "user"), ("b.md", "user")])
    evict_oldest(mem)
    evict_oldest(mem)
    assert len((tmp_path / "memory-evictions.jsonl").read_text().splitlines()) == 2


def test_failed_unlink_logs_nothing(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", "user"), ("b.md", "user")])

    def boom(self, *a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(type(mem / "a.md"), "unlink", boom)
    with pytest.raises(OSError):
        evict_oldest(mem)
    assert not (tmp_path / "memory-evictions.jsonl").exists()
    assert "a.md" in (mem / "MEMORY.md").read_text(encoding="utf-8")


def test_failed_row_drop_is_tolerated_after_unlink_and_log(tmp_path, monkeypatch):
    from coordinator_core.write_guards import _memory_evict

    mem = tmp_path / "memory"
    _seed(mem, [("a.md", "user"), ("b.md", "user")])

    def boom(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(_memory_evict, "_drop_rows", boom)
    rec = evict_oldest(mem)
    assert rec["name"] == "a.md" and not (mem / "a.md").exists()
    assert len((tmp_path / "memory-evictions.jsonl").read_text().splitlines()) == 1


def test_failed_log_write_still_drops_index_rows_then_raises(tmp_path):
    mem = tmp_path / "memory"
    _seed(mem, [("a.md", "user"), ("b.md", "user")])
    (tmp_path / "memory-evictions.jsonl").mkdir()
    with pytest.raises(OSError):
        evict_oldest(mem)
    assert not (mem / "a.md").exists()
    assert "a.md" not in (mem / "MEMORY.md").read_text(encoding="utf-8")


def test_oserror_propagates(tmp_path):
    with pytest.raises(OSError):
        evict_oldest(tmp_path / "missing")
