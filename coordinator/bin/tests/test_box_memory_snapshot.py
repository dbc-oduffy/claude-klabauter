"""Tests for coordinator/bin/box-memory-snapshot.py with injected readers; no real counters."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("box_memory_snapshot", BIN / "box-memory-snapshot.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
_hspec = importlib.util.spec_from_file_location("box_memory_host_t", BIN / "lib" / "box_memory_host.py")
host = importlib.util.module_from_spec(_hspec)
sys.modules["box_memory_host_t"] = host
_hspec.loader.exec_module(host)

GIB = host.GIB


def _status(avail=40, used=50, limit=100):
    return lambda: host.MemoryStatus(avail * GIB, 64 * GIB, used * GIB, limit * GIB)


def _cache():
    snap = SimpleNamespace(cache_bytes=2 * GIB, standby_bytes=5 * GIB)
    return lambda: SimpleNamespace(read_memory=lambda: snap)


GOV = SimpleNamespace(
    footprints=lambda: {1: 100.0, 2: 900.0, 3: 500.0},
    list_procs=lambda: [SimpleNamespace(pid=p, cmd=f"proc{p}") for p in (1, 2, 3)],
)


def _run(argv, **kw):
    kw.setdefault("status_fn", _status())
    kw.setdefault("nonpaged_fn", lambda: GIB)
    kw.setdefault("cache_host_fn", _cache())
    return mod.main(argv, platform="win32", governor=GOV, **kw)


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-home"))


def _admission(tmp_path, *lines):
    path = tmp_path / "admission.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return str(path)


def _start(pid, peak_mb=None, session="s", run="r"):
    line = {"phase": "START", "session_id": session, "run": run, "pids": [pid]}
    if peak_mb is not None:
        line["declared_peak_mb"] = peak_mb
    return line


def test_reserved_sums_live_unended_starts(tmp_path):
    path = _admission(
        tmp_path,
        _start(10, 4096, run="a"),
        _start(11, 4096, run="b"),
        {"phase": "END", "session_id": "s", "run": "b", "pids": []},
        _start(12, 4096, run="c"),
        _start(13, None, run="d"),
        _start(14, 1000, run="e"),
    )
    rss = {10: 1024.0, 12: 1024.0, 14: 5000.0}
    got = host.reserved_bytes(path, rss_fn=lambda p: rss[p], alive_fn=lambda p: p != 12)
    assert got == 3072 * 1024 * 1024


def test_reserved_missing_file_is_zero(tmp_path):
    assert host.reserved_bytes(tmp_path / "none.jsonl") == 0


def test_admit_peak_subtracts_reserved_both_sides(tmp_path, capsys):
    path = _admission(tmp_path, _start(10, 16 * 1024))
    kw = dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: True)
    assert _run(["--admit-peak", "10", "--admission-file", path], **kw) == 0
    capsys.readouterr()
    rc = _run(["--admit-peak", "25", "--admission-file", path], **kw)
    out = capsys.readouterr().out
    assert rc == 4 and "reserved=16.0GiB" in out and "available:" in out
    kw["alive_fn"] = lambda p: False
    assert _run(["--admit-peak", "25", "--admission-file", path], **kw) == 0


def test_admit(capsys):
    rc = _run(["--admit-peak", "10"])
    out = capsys.readouterr().out
    assert rc == 0 and "ADMIT" in out and "HOLD" not in out


def test_hold_available_names_clause(capsys):
    rc = _run(["--admit-peak", "40"])
    hold = [l for l in capsys.readouterr().out.splitlines() if l.startswith("HOLD")]
    assert rc == 4 and len(hold) == 1 and "available:" in hold[0] and "commit:" not in hold[0]


def test_hold_commit_with_limit_override(capsys):
    rc = _run(["--admit-peak", "10", "--commit-limit-gib", "55"])
    out = capsys.readouterr().out
    assert rc == 4 and "commit:" in out


def test_top_ordering_and_limit(capsys):
    assert _run(["--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "proc2" in out and "proc3" in out and "proc1" not in out
    assert out.index("proc2") < out.index("proc3")


def test_json_shape(capsys):
    assert _run(["--json", "--admit-peak", "10"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["available_gib"] == 40.0 and data["standby_gib"] == 5.0
    assert data["nonpaged_pool_gib"] == 1.0
    assert [r["pid"] for r in data["top_consumers"]] == [2, 3, 1]
    assert data["admit"]["verdict"] == "ADMIT"


def test_counter_failure_degrades_one_field(capsys):
    def boom():
        raise OSError("pdh")

    assert _run(["--json"], nonpaged_fn=boom) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["nonpaged_pool_gib"] == "unavailable"
    assert data["available_gib"] == 40.0


def test_admit_without_readable_status_exits_1(capsys):
    def boom():
        raise OSError("GlobalMemoryStatusEx")

    assert _run(["--admit-peak", "1"], status_fn=boom) == 1
    assert "cannot read memory status" in capsys.readouterr().err


def test_unsupported_host(capsys):
    rc = mod.main([], platform="linux")
    err = capsys.readouterr().err
    assert rc == 6 and "unsupported host" in err and len(err.strip().splitlines()) == 1


def _run_darwin(argv, **kw):
    kw.setdefault("status_fn", _status())
    return mod.main(argv, platform="darwin", governor=GOV, **kw)


def test_darwin_admit(capsys):
    assert _run_darwin(["--admit-peak", "4"]) == 0
    out = capsys.readouterr().out
    assert "ADMIT" in out and "unavailable" in out and "Available:        40.0 GiB" in out


def test_darwin_hold(capsys):
    assert _run_darwin(["--admit-peak", "39"]) == 4
    assert "HOLD" in capsys.readouterr().out


def test_help_exits_zero_on_linux(capsys):
    with pytest.raises(SystemExit) as exc:
        mod.main(["--help"], platform="linux")
    assert exc.value.code == 0
