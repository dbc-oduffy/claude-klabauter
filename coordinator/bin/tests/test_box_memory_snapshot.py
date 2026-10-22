"""Tests for coordinator/bin/box-memory-snapshot.py with injected readers; no real counters."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("box_memory_snapshot", BIN / "box-memory-snapshot.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
_hspec = importlib.util.spec_from_file_location("box_memory_host_t", BIN / "lib" / "box_memory_host.py")
host = importlib.util.module_from_spec(_hspec)
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


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_unsupported_host(plat, capsys):
    rc = mod.main([], platform=plat)
    err = capsys.readouterr().err
    assert rc == 6 and "unsupported host" in err and len(err.strip().splitlines()) == 1


def test_help_exits_zero_on_linux(capsys):
    with pytest.raises(SystemExit) as exc:
        mod.main(["--help"], platform="linux")
    assert exc.value.code == 0
