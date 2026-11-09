"""import_closure._read_text reads each path once per land_run deadline scope; with no scope it reads through."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.authoring_leaks import import_closure
from coordinator_core.ops.dispatch_emit import land_deadline


def _count_reads(monkeypatch) -> list:
    reads: list = []
    real = Path.read_text

    def spy(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    return reads


def test_scope_reads_a_path_once(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n", encoding="utf-8")
    reads = _count_reads(monkeypatch)
    with land_deadline.scope(60):
        assert import_closure._read_text(f) == "x = 1\n"
        assert import_closure._read_text(f) == "x = 1\n"
    assert reads == ["m.py"]


def test_none_result_is_memoised_in_scope(tmp_path, monkeypatch):
    missing = tmp_path / "gone.py"
    reads = _count_reads(monkeypatch)
    with land_deadline.scope(60):
        assert import_closure._read_text(missing) is None
        assert import_closure._read_text(missing) is None
    assert reads == ["gone.py"]


def test_new_scope_starts_empty(tmp_path, monkeypatch):
    f = tmp_path / "m.py"
    f.write_text("a\n", encoding="utf-8")
    with land_deadline.scope(60):
        assert import_closure._read_text(f) == "a\n"
    f.write_text("b\n", encoding="utf-8")
    with land_deadline.scope(60):
        assert import_closure._read_text(f) == "b\n"


def test_no_scope_reads_through(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a\n", encoding="utf-8")
    assert land_deadline._DEADLINE.get() is None
    assert import_closure._read_text(f) == "a\n"
    f.write_text("b\n", encoding="utf-8")
    assert import_closure._read_text(f) == "b\n"
