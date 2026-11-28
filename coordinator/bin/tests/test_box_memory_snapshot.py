"""Tests for coordinator/bin/box-memory-snapshot.py with injected readers; no real counters."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

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
CREATED = 1000.0


def _cmdline(pid):
    CMDLINE_CALLS.append(pid)
    return f"proc{pid}"


def _run(argv, local=True, **kw):
    if local:
        argv = [*argv, "--local"]
    kw.setdefault("status_fn", _status())
    kw.setdefault("nonpaged_fn", lambda: GIB)
    kw.setdefault("cache_host_fn", _cache())
    kw.setdefault("proc_table_fn", lambda: TABLE)
    kw.setdefault("cmdline_fn", _cmdline)
    kw.setdefault("create_time_fn", lambda p: CREATED)
    kw.setdefault("image_fn", lambda p: "python.exe")
    kw.setdefault("gpu_fn", lambda: {"unavailable": "nvml-absent"})
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
    line = {"phase": "START", "session_id": session, "run": run, "pids": [pid], "create_time": CREATED}
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
    got = host.reserved_bytes(
        path, rss_fn=lambda p: rss[p], alive_fn=lambda p: p != 12, create_time_fn=lambda p: CREATED,
    )
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


def _owner_kw(owner, **over):
    kw = dict(rss_fn=lambda p: 1024.0, alive_fn=lambda p: True, owner_pid_fn=lambda: owner)
    kw.update(over)
    return kw


def test_default_admit_reserves_for_owner_and_refuses_later_tenant(tmp_path, capsys):
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(500)) == 0
    capsys.readouterr()
    rc = _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(501))
    assert rc == 4 and "reserved=19.0GiB" in capsys.readouterr().out
    assert [j["pid"] for j in host.top_holders(path, rss_fn=lambda p: 1024.0, alive_fn=lambda p: True,
                                               create_time_fn=lambda p: CREATED, image_fn=lambda p: "python.exe")] == [500]


def test_owner_inner_admit_draws_from_its_own_reservation(tmp_path, capsys):
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(500)) == 0
    capsys.readouterr()
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(500)) == 0
    assert "reserved=0.0GiB" in capsys.readouterr().out
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(501)) == 4
    live, _ = host._ledger_starts(path)
    assert [r["pids"] for r in live.values()] == [[500]]


def test_owner_admit_keeps_its_other_key_reservation(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    base = ["--admission-file", path]
    assert _run(base + ["--command-key", "other", "--admit-peak", "10", "--register-pid", "500"],
                **_owner_kw(500)) == 0
    assert _run(base + ["--admit-peak", "10"], **_owner_kw(500)) == 0
    live, _ = host._ledger_starts(path)
    assert sorted(r["run"] for r in live.values()) == ["admit-peak:500", "other:500"]


def test_dead_owner_default_reservation_is_reclaimed(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(500)) == 0
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(501)) == 4
    assert _run(["--admit-peak", "20", "--admission-file", path],
                **_owner_kw(501, alive_fn=lambda p: p != 500)) == 0


def test_owner_procstart_change_default_reservation_is_reclaimed(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(500)) == 0
    assert _run(["--admit-peak", "20", "--admission-file", path], **_owner_kw(501)) == 4
    assert _run(["--admit-peak", "20", "--admission-file", path],
                **_owner_kw(501, create_time_fn=lambda p: CREATED + 500.0)) == 0


def test_held_default_admit_registers_nothing(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admit-peak", "39", "--admission-file", path], **_owner_kw(500)) == 4
    assert host.reserved_bytes(path, rss_fn=lambda p: 0.0, alive_fn=lambda p: True,
                               create_time_fn=lambda p: CREATED) == 0


def test_admit_need_is_peak_plus_cushion_with_no_floor(tmp_path, capsys):
    # avail 40: peak 31 needs 38.75 and admits; peak 33 needs 41 and holds.
    adm = str(tmp_path / "a.jsonl")
    assert _run(["--admit-peak", "31", "--admission-file", adm]) == 0
    assert "need_avail=38.75GiB" in capsys.readouterr().out
    assert _run(["--admit-peak", "33", "--admission-file", adm]) == 4
    assert "available:" in capsys.readouterr().out


def test_recorded_peak_keeps_max_and_drives_admit(tmp_path, capsys):
    path = str(tmp_path / "admission.jsonl")
    base = ["--admission-file", path, "--command-key", "k"]
    _run(base + ["--record-peak", "3"])
    _run(base + ["--record-peak", "1"])
    assert host.read_recorded_peak(path, "k") == 3.0
    capsys.readouterr()
    assert _run(base) == 0
    assert "peak=3.0GiB" in capsys.readouterr().out


def test_reservation_flag_prints_per_job_and_total_exited_adds_zero(tmp_path, capsys):
    path = _admission(
        tmp_path,
        _start(10, 4096, run="a"),
        _start(11, 8192, run="b"),
        _start(12, 2048, run="c"),
        {"phase": "END", "session_id": "s", "run": "c", "pids": []},
    )
    rss = {10: 1024.0, 11: 9000.0, 12: 0.0}
    kw = dict(rss_fn=lambda p: rss[p], alive_fn=lambda p: p != 11)
    assert _run(["--reservation", "--admission-file", path, "--json"], **kw) == 0
    res = json.loads(capsys.readouterr().out)["reservation"]
    assert [(j["pid"], j["reserved_gib"]) for j in res["jobs"]] == [(10, 3.0)] and res["total_gib"] == 3.0
    kw["alive_fn"] = lambda p: p == 11 or p == 10
    assert _run(["--reservation", "--admission-file", path], **kw) == 0
    out = capsys.readouterr().out
    assert "Reservation total: 3.0 GiB" in out and "ADMIT" not in out
    kw["rss_fn"] = lambda p: 0.0
    assert _run(["--reservation", "--admission-file", path, "--json"], **kw) == 0
    res = json.loads(capsys.readouterr().out)["reservation"]
    assert res["total_gib"] == 12.0 and len(res["jobs"]) == 2


def test_reservation_with_admit_peak_keeps_hold_exit_4(tmp_path, capsys):
    path = _admission(tmp_path, _start(10, 16 * 1024))
    kw = dict(rss_fn=lambda p: 0.0, alive_fn=lambda p: True)
    rc = _run(["--reservation", "--admit-peak", "25", "--admission-file", path], **kw)
    out = capsys.readouterr().out
    assert rc == 4 and "Reservation total: 16.0 GiB" in out and "reserved=16.0GiB" in out


def _grant_layout(tmp_path, peak_mb, *, run="reseal", expires_in_s=1800):
    import time
    from datetime import datetime, timezone

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    now = time.time()
    gdir = tmp_path / "state" / "group-em"
    (gdir / "slot-grants" / "issuer").mkdir(parents=True)
    (gdir / "slot-grants" / "issuer" / f"grantee.{run}.json").write_text(json.dumps({
        "kind": "group-em-slot-grant", "grantee_session_id": "grantee", "run": run,
        "declared_peak_mb": peak_mb, "issued_at": iso(now - 60), "expires_at": iso(now + expires_in_s),
    }), encoding="utf-8")
    return str(gdir / "box" / "admission.jsonl")


def test_admit_peak_reserves_unregistered_slot_grant_and_holds(tmp_path, capsys):
    path = _grant_layout(tmp_path, 19 * 1024)
    # No admission file at all: only the grant reserves. avail 40, peak 20 + 25% cushion = 25.
    rc = _run(["--admit-peak", "20", "--admission-file", path])
    out = capsys.readouterr().out
    assert rc == 4 and "reserved=19.0GiB" in out and "HOLD" in out
    # Same admit with no grant is admitted: the HOLD comes from the grant.
    empty = str(tmp_path / "other" / "box" / "admission.jsonl")
    assert _run(["--admit-peak", "20", "--admission-file", empty]) == 0


def test_expired_slot_grant_reserves_nothing(tmp_path, capsys):
    path = _grant_layout(tmp_path, 19 * 1024, expires_in_s=-30)
    assert _run(["--admit-peak", "20", "--admission-file", path]) == 0
    assert "reserved=0.0GiB" in capsys.readouterr().out


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


def test_admit_without_readable_status_exits_error_code(capsys):
    def boom():
        raise OSError("GlobalMemoryStatusEx")

    assert _run(["--admit-peak", "1"], status_fn=boom) == mod.EXIT_ERROR == 5
    assert "cannot read memory status" in capsys.readouterr().err


def test_json_unreadable_status_prints_typed_error(capsys):
    def boom():
        raise OSError("GlobalMemoryStatusEx")

    assert _run(["--json", "--admit-peak", "12"], status_fn=boom) == 5
    out = capsys.readouterr().out
    admit = json.loads(out)["admit"]
    assert admit["verdict"] == "ERROR" and "GlobalMemoryStatusEx" in admit["error"] and admit["peak_gib"] == 12.0


def test_json_exception_inside_admit_prints_typed_error(capsys, monkeypatch):
    real_load = mod._load

    def load(name):
        h = real_load(name)
        monkeypatch.setattr(h, "admit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("admit exploded")))
        return h

    monkeypatch.setattr(mod, "_load", load)
    assert _run(["--json", "--admit-peak", "12"]) == 5
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {"admit": {"verdict": "ERROR", "error": "RuntimeError: admit exploded"}}
    assert "admit exploded" in cap.err


def test_non_json_exception_still_propagates(monkeypatch):
    monkeypatch.setattr(mod, "_load", lambda name: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        _run(["--admit-peak", "12"])


def test_json_hold_and_admit_unchanged(capsys):
    assert _run(["--json", "--admit-peak", "10"]) == 0
    assert json.loads(capsys.readouterr().out)["admit"]["verdict"] == "ADMIT"
    assert _run(["--json", "--admit-peak", "90"]) == 4
    assert json.loads(capsys.readouterr().out)["admit"]["verdict"] == "HOLD"


def test_admit_refusal_names_the_reader_failure(capsys):
    def boom():
        raise OSError("cannot import coordinator_core.telemetry.host_sampler")

    assert _run(["--admit-peak", "1"], status_fn=boom) == 5
    err = capsys.readouterr().err
    assert "cannot read memory status for admission" in err
    assert "coordinator_core.telemetry.host_sampler" in err


def test_unsupported_host(capsys):  # linux is supported; any other OS is not
    rc = mod.main([], platform="freebsd")
    err = capsys.readouterr().err
    assert rc == 6 and "unsupported host" in err and len(err.strip().splitlines()) == 1


def _run_darwin(argv, **kw):
    argv = [*argv, "--local"]
    kw.setdefault("status_fn", _status())
    kw.setdefault("gpu_fn", lambda: {"unavailable": "nvml-absent"})
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


def test_fleet_admits_the_sized_fleet_the_default_cushion_holds(tmp_path, capsys):
    """MetaHuman 2026-10-29: a 43.5 GiB fleet, reserve S already out, waited for 51.5 GiB."""
    adm = str(tmp_path / "a.jsonl")
    status = _status(avail=43.5, used=10, limit=100)
    assert _run(["--admit-peak", "43.5", "--fleet", "--admission-file", adm], status_fn=status) == 0
    assert "ADMIT" in capsys.readouterr().out
    assert _run(["--admit-peak", "43.5", "--admission-file", adm], status_fn=status) == 4
    assert "HOLD" in capsys.readouterr().out


def test_fleet_still_holds_on_available_and_on_commit(tmp_path, capsys):
    adm = str(tmp_path / "a.jsonl")
    assert _run(["--admit-peak", "43.6", "--fleet", "--admission-file", adm],
                status_fn=_status(avail=43.5, used=10, limit=100)) == 4
    assert "available:" in capsys.readouterr().out
    assert _run(["--admit-peak", "43.5", "--fleet", "--admission-file", adm],
                status_fn=_status(avail=60, used=60, limit=100)) == 4
    assert "commit:" in capsys.readouterr().out


def test_fleet_drops_the_cushion_on_the_register_pid_path(tmp_path, capsys):
    adm = str(tmp_path / "a.jsonl")
    rc = _run(["--admit-peak", "43.5", "--fleet", "--admission-file", adm, "--command-key", "mh",
               "--register-pid", "4242"], status_fn=_status(avail=43.5, used=10, limit=100))
    assert rc == 0 and "ADMIT" in capsys.readouterr().out


def test_help_names_which_mode_fits_which_case(capsys):
    with pytest.raises(SystemExit):
        _run(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "single heavy job" in out and "already sized with its reserve S" in out


def _boom():
    raise OSError("commit exhausted")


def _img(*names):
    return [SimpleNamespace(pid=i, private_bytes=0, image=n) for i, n in enumerate(names)]


def test_every_memory_read_failing_still_prints_process_counts(capsys):
    rc = _run(
        ["--json"],
        status_fn=_boom,
        nonpaged_fn=_boom,
        cache_host_fn=_boom,
        proc_table_fn=_boom,
        names_fn=lambda: ["python.exe", "Python.exe", "conhost.exe", "svchost.exe"],
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["process_counts"] == {"python": 2, "conhost": 1}
    assert data["available_gib"] == "unavailable" and data["top_consumers"] == "unavailable"
    assert "not evidence of a kernel leak" in data["note"]


def test_default_text_output_names_counts_and_note_when_reads_fail(capsys):
    _run([], status_fn=_boom, proc_table_fn=_boom, names_fn=lambda: ["python.exe", "conhost.exe"])
    out = capsys.readouterr().out
    assert "python=1  conhost=1" in out and "kernel leak" in out


def test_counts_come_from_the_process_table_when_it_reads(capsys):
    def no_psutil():
        raise AssertionError("psutil fallback used while the table read")

    table = _img("python.exe", "pythonw.exe", "conhost.exe", "conhost.exe", "cmd.exe")
    _run(["--json"], proc_table_fn=lambda: table, names_fn=no_psutil)
    data = json.loads(capsys.readouterr().out)
    assert data["process_counts"] == {"python": 2, "conhost": 2}
    assert "note" not in data


def test_counts_unavailable_when_table_and_psutil_both_fail(capsys):
    _run(["--json"], proc_table_fn=_boom, names_fn=_boom)
    assert json.loads(capsys.readouterr().out)["process_counts"] == "unavailable"


_NVML = {
    "gpus": [
        {"index": 0, "name": "A", "vram_total_mib": 24576, "vram_used_mib": 4096, "util_pct": 3},
        {"index": 1, "name": "B", "vram_total_mib": 8192, "vram_used_mib": 8000, "util_pct": 0},
    ],
    "vram_by_pid": {10: 1024, 11: 1023, 12: 6000, 13: 0},
}
_UNIFIED = {
    "gpus": [{"index": 0, "name": "Apple silicon", "vram_total_mib": 32768, "vram_used_mib": None, "unified": True}],
    "vram_by_pid": {},
}


def test_gpu_object_nvml_free_total_and_one_gib_job_filter():
    gpu = mod._gpu_object(_NVML)
    assert gpu["vram_total_mib"] == 24576 + 8192
    assert gpu["vram_free_mib"] == (24576 - 4096) + (8192 - 8000)
    assert [d["vram_free_mib"] for d in gpu["devices"]] == [20480, 192]
    assert gpu["jobs"] == [{"pid": 12, "vram_mib": 6000}, {"pid": 10, "vram_mib": 1024}]


def test_gpu_object_unified_reading_has_total_and_no_free():
    gpu = mod._gpu_object(_UNIFIED)
    assert gpu["vram_total_mib"] == 32768 and gpu["vram_free_mib"] is None
    assert gpu["devices"][0]["unified"] is True and gpu["jobs"] == []


def test_gpu_object_unavailable_reading_names_the_reason():
    assert mod._gpu_object({"gpu_unavailable": "nvml-absent"}) == {"unavailable": "nvml-absent"}
    assert "unavailable" in mod._gpu_object({})


def test_json_carries_gpu_object_from_one_reading(capsys):
    calls = []
    assert _run(["--json"], gpu_fn=lambda: calls.append(1) or mod._gpu_object(_NVML)) == 0
    data = json.loads(capsys.readouterr().out)
    assert calls == [1] and data["gpu"]["vram_free_mib"] == 20672
    assert [j["pid"] for j in data["gpu"]["jobs"]] == [12, 10]


def test_json_gpu_unavailable_reason_and_other_fields_intact(capsys):
    assert _run(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["gpu"] == {"unavailable": "nvml-absent"} and data["available_gib"] == 40.0


def test_text_render_shows_gpu_free_total_and_jobs(capsys):
    assert _run([], gpu_fn=lambda: mod._gpu_object(_NVML)) == 0
    out = capsys.readouterr().out
    assert "GPU VRAM:" in out and "20.19 GiB free / 32.0 GiB" in out and "12=5.86 GiB" in out
    assert "13=" not in out and "11=" not in out


def test_text_render_gpu_unavailable_prints_reason(capsys):
    assert _run([]) == 0
    assert "GPU:              unavailable: nvml-absent" in capsys.readouterr().out


def test_darwin_json_gpu_unified(capsys):
    assert _run_darwin(["--json"], gpu_fn=lambda: mod._gpu_object(_UNIFIED)) == 0
    assert json.loads(capsys.readouterr().out)["gpu"]["vram_total_mib"] == 32768


def test_read_gpu_failure_degrades_to_unavailable(monkeypatch):
    monkeypatch.setattr(mod, "_place_engine_on_path", _boom)
    assert mod._read_gpu() == {"unavailable": "gpu-read-error:OSError"}


# ---- fleet-wide default and --local ------------------------------------------------------------

_NOW = 1_000_000.0
_GIPSY = {
    "box": "box-g", "alias": "gipsy", "os": "windows", "state": "ok", "cores": 16, "ram_total_mb": 65536,
    "commit_limit_mb": 98304, "gpus": [{"name": "RTX 4090", "vram_total_mib": 24576}], "ram_free_mb": 30720,
    "commit_used_mb": 40960, "vram_used_mib": 2048, "sessions": 3, "jobs": [], "last_seen": _NOW - 30,
    "maxima_source": None,
}
_EXAMPLE_FLEET = {**_GIPSY, "box": "box-d", "alias": "machine-b", "ram_free_mb": 10240}
_MINI = {
    "box": "box-m", "alias": "mini", "os": "darwin", "state": "timed_out", "reason": "ssh_timeout", "cores": 8,
    "ram_total_mb": 16384, "commit_limit_mb": None, "gpus": None, "last_seen": _NOW - 3 * 86400,
    "maxima_source": "last_known",
}
_NAMES = lambda: ["python.exe", "conhost.exe", "python3.11"]  # noqa: E731


def _fleet(*rows):
    return lambda: {"local_alias": "machine-a", "rows": list(rows)}


def _no_fleet():
    raise AssertionError("the fleet fan-out must not run")


def _fleet_run(argv, fleet_fn, **kw):
    return _run(argv, local=False, fleet_fn=fleet_fn, names_fn=_NAMES, **kw)


def test_local_flag_is_todays_single_box_output_byte_for_byte(capsys):
    assert _run(["--top", "2"], fleet_fn=_no_fleet, names_fn=_NAMES) == 0
    assert capsys.readouterr().out == (
        "Available:        40.0 GiB\n"
        "Commit:           50.0 / 100.0 GiB\n"
        "System cache WS:  2.0 GiB\n"
        "Standby:          5.0 GiB\n"
        "Nonpaged pool:    1.0 GiB\n"
        "Process counts:   python=2  conhost=1\n"
        "GPU:              unavailable: nvml-absent\n"
        "Top consumers by private commit:\n"
        "        2      900.0 MB  proc2\n"
        "        3      500.0 MB  proc3\n"
    )


def test_default_prints_a_section_per_box_and_keeps_the_unreachable_box(monkeypatch, capsys):
    monkeypatch.setattr(mod.time, "time", lambda: _NOW)
    assert _fleet_run([], _fleet(_GIPSY, _MINI)) == 0
    out = capsys.readouterr().out
    assert out.startswith("== machine-a (this box) ==\nAvailable:        40.0 GiB")
    assert "== gipsy (windows) ==\nAvailable:        30.0 GiB" in out
    assert "Commit:           40.0 / 96.0 GiB" in out
    assert "Sessions/jobs:    3 / 0" in out
    assert "Maxima:           16 cores, 64.0 GiB RAM, commit limit 96.0 GiB, RTX 4090 24.0 GiB" in out
    assert "== mini (darwin) UNREACHABLE (timed out) (ssh_timeout) ==" in out
    assert "Maxima (last known): 8 cores, 16.0 GiB RAM" in out
    assert "Last seen:        3d ago" in out
    assert out.rstrip().endswith("power it on? mini is UNREACHABLE (last seen 3d ago)")


def test_default_admit_peak_reports_each_box_and_exit_is_this_boxs(capsys):
    assert _fleet_run(["--admit-peak", "20"], _fleet(_GIPSY, _EXAMPLE_FLEET, _MINI)) == 0
    out = capsys.readouterr().out
    gipsy, example_fleet, mini = (out.split("== ")[i] for i in (2, 3, 4))
    assert "ADMIT peak=20.0GiB" in gipsy and "reserved=n/a" in gipsy
    assert "HOLD peak=20.0GiB failed available: need 25.0GiB > avail 10.0GiB" in example_fleet
    assert "UNKNOWN peak=20.0GiB (ssh_timeout)" in mini
    assert "ADMIT peak=20.0GiB need_avail=25.0GiB reserved=0.0GiB" in out.split("== ")[1]


def test_default_admit_peak_exit_stays_this_boxs_hold(capsys):
    assert _fleet_run(["--admit-peak", "39"], _fleet(_GIPSY)) == 4
    out = capsys.readouterr().out
    assert "HOLD" in out.split("== ")[1] and "HOLD peak=39.0GiB failed available: need 47.0GiB > avail 30.0GiB" in out.split("== ")[2]


def test_far_row_with_a_missing_reading_is_unknown_never_admit(capsys):
    assert _fleet_run(["--admit-peak", "1"], _fleet({**_GIPSY, "commit_used_mb": None})) == 0
    assert "UNKNOWN peak=1.0GiB (reading missing)" in capsys.readouterr().out


def test_fleet_flag_still_drops_the_cushion_on_far_boxes(capsys):
    assert _fleet_run(["--admit-peak", "30", "--fleet"], _fleet(_GIPSY)) == 0
    assert "ADMIT peak=30.0GiB need_avail=30.0GiB" in capsys.readouterr().out.split("== ")[2]


def test_json_keeps_single_box_fields_and_adds_fleet_rows(capsys):
    assert _fleet_run(["--json", "--admit-peak", "20"], _fleet(_GIPSY, _MINI)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["available_gib"] == 40.0 and doc["admit"]["verdict"] == "ADMIT"
    by_alias = {r["alias"]: r for r in doc["fleet"]}
    assert by_alias["gipsy"]["admit"]["verdict"] == "ADMIT" and by_alias["gipsy"]["ram_free_mb"] == 30720
    assert by_alias["mini"]["state"] == "timed_out" and by_alias["mini"]["cores"] == 8
    assert by_alias["mini"]["admit"] == {"verdict": "UNKNOWN", "peak_gib": 20.0, "reason": "ssh_timeout"}
    assert doc["power_on"] == ["box-m"]


@pytest.mark.parametrize("fleet_fn", [lambda: {"error": "OSError: engine absent"}, lambda: 1 / 0])
def test_a_failed_fleet_read_never_sinks_this_boxs_snapshot(fleet_fn, capsys):
    assert _fleet_run([], fleet_fn) == 0
    out = capsys.readouterr().out
    assert "Available:        40.0 GiB" in out and "== fleet == unavailable (" in out


@pytest.mark.parametrize("extra", [
    ["--command-key", "k", "--register-pid", "5", "--admit-peak", "1"],
    ["--command-key", "k", "--release-pid", "5"],
    ["--command-key", "k", "--record-peak", "2"],
    ["--reservation"],
    ["--admission-file", "x.jsonl"],
])
def test_admission_state_flags_stay_this_box_and_never_fan_out(tmp_path, extra, capsys):
    if "--admission-file" in extra:
        extra = ["--admission-file", _admission(tmp_path)]
    elif "--reservation" not in extra:
        extra = [*extra, "--admission-file", _admission(tmp_path)]
    rc = _run(extra, local=False, fleet_fn=_no_fleet, names_fn=_NAMES)
    assert rc in (0, 4) and "==" not in capsys.readouterr().out


# Real Ubuntu readings (24 CPUs, kernel 7.0.0-44).
MEMINFO = """MemTotal:       95954360 kB
MemFree:        60000000 kB
MemAvailable:   91225216 kB
Buffers:          500000 kB
Cached:          9000000 kB
SwapCached:            0 kB
SwapTotal:       8388604 kB
SwapFree:        8388604 kB
"""
PSI = """some avg10=0.00 avg60=0.00 avg300=0.00 total=36
full avg10=0.00 avg60=0.00 avg300=0.00 total=34
"""


def test_memory_status_linux_from_meminfo_fixture():
    st = host.memory_status_linux(MEMINFO)
    assert st.total_phys == 95954360 * 1024 and st.avail_phys == 91225216 * 1024
    assert st.commit_limit == (95954360 + 8388604) * 1024
    assert st.commit_used == (95954360 - 91225216) * 1024  # swap untouched


def test_memory_status_linux_counts_swap_in_use():
    st = host.memory_status_linux(MEMINFO.replace("SwapFree:        8388604", "SwapFree:        388604"))
    assert st.commit_used == (95954360 - 91225216 + 8000000) * 1024


def test_memory_status_linux_without_memavailable_raises():
    with pytest.raises(OSError):
        host.memory_status_linux("MemTotal: 1024 kB\n")


def test_linux_cached_and_psi_parse():
    assert host.linux_cached_bytes(MEMINFO) == 9000000 * 1024
    assert host.read_psi_memory(PSI) == {"some_avg10": 0.0, "full_avg10": 0.0}


def _run_linux(argv, **kw):
    kw.setdefault("status_fn", lambda: host.memory_status_linux(MEMINFO))
    kw.setdefault("cache_host_fn", lambda: SimpleNamespace(read_memory=lambda: SimpleNamespace(
        cache_bytes=host.linux_cached_bytes(MEMINFO), standby_bytes=None)))
    kw.setdefault("gpu_fn", lambda: {"unavailable": "nvml-absent"})
    return mod.main([*argv, "--local"], platform="linux", governor=GOV, **kw)


def test_linux_snapshot_and_admit(capsys):
    assert _run_linux(["--admit-peak", "20"], owner_pid_fn=lambda: 0) == 0
    out = capsys.readouterr().out
    assert "ADMIT" in out and "Available:        87.0 GiB" in out and "Standby:          unavailable" in out
    assert "System cache WS:  8.58 GiB" in out


def test_linux_hold_when_peak_plus_cushion_exceeds_available(capsys):
    assert _run_linux(["--admit-peak", "80"], owner_pid_fn=lambda: 0) == 4  # 80 + 8 GiB cushion > 87.0 GiB available
    assert "HOLD" in capsys.readouterr().out


def test_linux_default_admit_registers_owner_reservation(tmp_path):
    path = str(tmp_path / "admission.jsonl")
    kw = dict(rss_fn=lambda p: 1024.0, alive_fn=lambda p: True, create_time_fn=lambda p: CREATED,
              image_fn=lambda p: "python.exe")
    assert _run_linux(["--admit-peak", "40", "--admission-file", path], owner_pid_fn=lambda: 500, **kw) == 0
    assert _run_linux(["--admit-peak", "60", "--admission-file", path], owner_pid_fn=lambda: 501, **kw) == 4

