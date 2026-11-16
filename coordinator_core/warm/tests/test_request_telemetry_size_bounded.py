"""flush_requests keeps request-telemetry.jsonl and its rotated generations under a fixed byte bound."""

import pytest

from coordinator_core.warm import telemetry
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(telemetry, "_REQUESTS", telemetry.collections.deque(maxlen=telemetry.REQUEST_TELEMETRY_KEEP))
    monkeypatch.setattr(telemetry, "request_telemetry_path", lambda engine_root=None: tmp_path / "request-telemetry.jsonl")
    return tmp_path


def _all_generations(tmp_path):
    return sorted(tmp_path.glob("request-telemetry*.jsonl"))


def _flush_rows(n) -> None:
    for i in range(n):
        telemetry.record_request(op="x" * 200, session="s", outcome="ok", pid=i)
    telemetry.flush_requests()


def test_repeated_flush_never_exceeds_cap_or_generation_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cap = 4096
    monkeypatch.setattr(telemetry, "REQUEST_TELEMETRY_MAX_BYTES", cap)
    for _ in range(60):
        _flush_rows(8)
        files = _all_generations(tmp_path)
        assert all(f.stat().st_size <= cap for f in files)
        assert len(files) <= telemetry.REQUEST_TELEMETRY_GENERATIONS + 1
    total = sum(f.stat().st_size for f in _all_generations(tmp_path))
    assert total <= cap * (telemetry.REQUEST_TELEMETRY_GENERATIONS + 1)
    assert len(_all_generations(tmp_path)) > 1, "rotation never fired"


def test_oversized_legacy_file_is_not_retained_as_a_generation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cap = 4096
    monkeypatch.setattr(telemetry, "REQUEST_TELEMETRY_MAX_BYTES", cap)
    live = tmp_path / "request-telemetry.jsonl"
    live.write_bytes(b"y" * (cap * 50) + b"\n")

    _flush_rows(2)

    files = _all_generations(tmp_path)
    assert files and all(f.stat().st_size <= cap for f in files)
    assert 0 < live.stat().st_size <= cap


def test_below_cap_flush_appends_without_rotating(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(telemetry, "REQUEST_TELEMETRY_MAX_BYTES", 1024 * 1024)
    _flush_rows(3)
    _flush_rows(3)
    files = _all_generations(tmp_path)
    assert [f.name for f in files] == ["request-telemetry.jsonl"]
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 6
