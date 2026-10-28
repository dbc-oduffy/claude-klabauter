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


MB = 1024**2
TABLE = [
    SimpleNamespace(pid=1, private_bytes=100 * MB),
    SimpleNamespace(pid=2, private_bytes=900 * MB),
    SimpleNamespace(pid=3, private_bytes=500 * MB),
]
CMDLINE_CALLS: list = []


def _cmdline(pid):
    CMDLINE_CALLS.append(pid)
    return f"proc{pid}"


def _run(argv, **kw):
    kw.setdefault("status_fn", _status())
    kw.setdefault("nonpaged_fn", lambda: GIB)
    kw.setdefault("cache_host_fn", _cache())
    kw.setdefault("proc_table_fn", lambda: TABLE)
    kw.setdefault("cmdline_fn", _cmdline)
    kw.setdefault("floor_fn", lambda: 0)
    CMDLINE_CALLS.clear()
    return mod.main(argv, platform="win32", **kw)


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


def test_registered_command_reservation_holds_second(tmp_path, capsys):
    path = str(tmp_path / "admission.jsonl")
    kw = dict(rss_fn=lambda p: 1024.0, alive_fn=lambda p: True)
    base = ["--admission-file", path, "--command-key", "k"]
    assert _run(base + ["--admit-peak", "20", "--register-pid", "77"], **kw) == 0
    capsys.readouterr()
    # peak 20 fits avail 40, but 20 - 1 GiB RSS stays reserved: 40 - 19 = 21 < 1.25 * 20.
    rc = _run(["--admission-file", path, "--admit-peak", "20"], **kw)
    out = capsys.readouterr().out
    assert rc == 4 and "reserved=19.0GiB" in out
    # release frees it
    assert _run(base + ["--release-pid", "77", "--admit-peak", "20"], **kw) == 0


def test_dead_holder_reservation_freed(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    base = ["--admission-file", path, "--command-key", "k"]
    kw = dict(rss_fn=lambda p: 0.0)
    assert _run(base + ["--admit-peak", "20", "--register-pid", "77"], alive_fn=lambda p: True, **kw) == 0
    assert _run(["--admission-file", path, "--admit-peak", "20"], alive_fn=lambda p: True, **kw) == 4
    assert _run(["--admission-file", path, "--admit-peak", "20"], alive_fn=lambda p: False, **kw) == 0


def test_hold_when_peak_fits_but_not_floor_plus_peak(tmp_path, capsys):
    # avail 40: peak 31 needs 38.75 by cushion (fits) but 10 + 31 = 41 with a 10 GiB floor (does not).
    adm = ["--admit-peak", "31", "--admission-file", str(tmp_path / "a.jsonl")]
    assert _run(adm, floor_fn=lambda: 10 * GIB) == 4
    assert "available:" in capsys.readouterr().out
    assert _run(adm, floor_fn=lambda: 0) == 0


def test_recorded_peak_keeps_max_and_drives_admit(tmp_path, capsys):
    path = str(tmp_path / "admission.jsonl")
    base = ["--admission-file", path, "--command-key", "k"]
    _run(base + ["--record-peak", "3"])
    _run(base + ["--record-peak", "1"])
    assert host.read_recorded_peak(path, "k") == 3.0
    capsys.readouterr()
    assert _run(base) == 0
    assert "peak=3.0GiB" in capsys.readouterr().out


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
    assert sorted(CMDLINE_CALLS) == [2, 3]


def test_top_proc_table_failure_degrades_one_field(capsys):
    def boom():
        raise OSError("ntquery")

    assert _run(["--json"], proc_table_fn=boom) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["top_consumers"] == "unavailable" and data["available_gib"] == 40.0


def test_win32_never_loads_governor(monkeypatch, capsys):
    def no_gov():
        raise AssertionError("load_governor called on win32")

    monkeypatch.setattr(host, "load_governor", no_gov)
    monkeypatch.setattr(mod, "_load", lambda name: host)
    assert _run(["--json"]) == 0
    assert [r["pid"] for r in json.loads(capsys.readouterr().out)["top_consumers"]] == [2, 3, 1]


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
