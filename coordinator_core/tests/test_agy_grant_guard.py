"""Agy grant tamper guard: marker reaches engine.drift; hashing sees content change under a restored mtime."""
import os

from coordinator_core import agy_grant_guard as guard
from coordinator_core.ops import engine_drift


def test_content_change_is_seen_even_when_size_and_mtime_are_restored(tmp_path) -> None:
    f = tmp_path / "a.md"
    f.write_text("aaaa", encoding="utf-8")
    stat = f.stat()
    before = guard.snapshot([tmp_path])
    f.write_text("bbbb", encoding="utf-8")
    os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert guard.changed_paths(before, guard.snapshot([tmp_path])) == [str(f)]


def test_engine_drift_reports_the_tamper_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(engine_drift, "probe_engine_drift", lambda: {"state": "ok"})
    assert "agy_grant_tamper" not in engine_drift._engine_drift({})
    guard.write_marker(["x"])
    assert engine_drift._engine_drift({})["agy_grant_tamper"]["paths"] == ["x"]
