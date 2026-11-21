"""fire_stop / workflow.cancel: the run's headless fire is identity-verified, then stopped leaf-first."""

import json

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import fire_stop, halted_run as hr, live_run_index as idx, run_cancel
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.session_hub_path import hub_for_common

SIZING = "state/sizings/2026-11/2026-11-15-foo.yaml"
RUN = "warp-2026-11-15-aaaaaa-1-foo-20261115T000000-bbbbbb"
DRIVER = 100
BORN = 5000.0


class _Gone(Exception):
    pass


class _Denied(Exception):
    pass


class FakePs:
    """A process table: pid -> {ppid, argv, created}. `immortal` pids ignore terminate and kill."""

    NoSuchProcess = _Gone
    AccessDenied = _Denied

    def __init__(self, table, immortal=(), vanish_on_ppid=()):
        self.table, self.immortal, self.ended = dict(table), set(immortal), []
        self.vanish_on_ppid = set(vanish_on_ppid)
        ps = self

        class Process:
            def __init__(self, pid):
                if pid not in ps.table:
                    raise _Gone(pid)
                self.pid = pid

            def cmdline(self):
                return ps.table[self.pid]["argv"]

            def create_time(self):
                return ps.table[self.pid]["created"]

            def ppid(self):
                if self.pid in ps.vanish_on_ppid:
                    ps.table.pop(self.pid, None)
                    raise _Gone(self.pid)
                return ps.table[self.pid]["ppid"]

            def children(self, recursive=False):
                out, frontier = [], [self.pid]
                while frontier:
                    kids = [p for p, row in ps.table.items() if row["ppid"] in frontier]
                    out += [Process(p) for p in kids]
                    frontier = kids if recursive else []
                return out

            def terminate(self):
                if self.pid not in ps.immortal and self.pid in ps.table:
                    ps.ended.append(self.pid)
                    del ps.table[self.pid]

            kill = terminate

        self.Process = Process

    def pid_exists(self, pid):
        return pid in self.table

    def wait_procs(self, procs, timeout=None):
        return [p for p in procs if p.pid not in self.table], [p for p in procs if p.pid in self.table]


def _argv(record_path):
    return ["python", "-P", "-m", fire_stop.DRIVER_MODULE, "--record", str(record_path)]


def _table(record_path):
    return {
        DRIVER: {"ppid": 1, "argv": _argv(record_path), "created": BORN},
        201: {"ppid": DRIVER, "argv": ["node", "runtime.mjs"], "created": BORN + 1},
        301: {"ppid": 201, "argv": ["codex.exe"], "created": BORN + 2},
        401: {"ppid": 301, "argv": ["rg"], "created": BORN + 3},
        900: {"ppid": 1, "argv": ["bystander"], "created": BORN},
    }


def _fire(root, record_path_name="f" * 32, **extra):
    registry = hub_for_common(root / ".git") / "workflow-fires"
    registry.mkdir(parents=True, exist_ok=True)
    path = registry / f"{record_path_name}.json"
    record = {"fire_id": record_path_name, "run_id": RUN, "state": "running", "pid": DRIVER,
              "pid_birth": BORN, **extra}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path, record


@pytest.fixture(autouse=True)
def _isolated_machine_local(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def test_tree_is_stopped_leaf_first_and_driver_last(tmp_path):
    path, record = _fire(tmp_path)
    ps = FakePs(_table(path))
    out = fire_stop.stop_fire(path, record, ps)
    assert ps.ended == [401, 301, 201, DRIVER]
    assert out["survivors"] == [] and out["stopped"] == [DRIVER, 201, 301, 401]
    assert 900 in ps.table


def test_vanished_child_mid_walk_does_not_abort_the_driver_stop(tmp_path):
    path, record = _fire(tmp_path)
    ps = FakePs(_table(path), vanish_on_ppid={201})
    out = fire_stop.stop_fire(path, record, ps)
    assert DRIVER in ps.ended and 401 in ps.ended and 301 in ps.ended
    assert out["survivors"] == [] and DRIVER in out["stopped"]
    assert 900 in ps.table


def test_driver_surviving_terminate_and_kill_is_a_survivor_not_stopped(tmp_path):
    path, record = _fire(tmp_path)
    ps = FakePs(_table(path), immortal={DRIVER})
    out = fire_stop.stop_fire(path, record, ps)
    assert out["survivors"] == [DRIVER] and DRIVER not in out["stopped"]


def test_cancel_keeps_run_indexed_when_driver_survives(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    assert idx.claim(tmp_path, RUN, ["a.py"]) == {}
    path, _ = _fire(tmp_path)
    ps = FakePs(_table(path), immortal={DRIVER}, vanish_on_ppid={201})
    real = fire_stop.stop_run_fires
    monkeypatch.setattr(fire_stop, "stop_run_fires", lambda r, run: real(r, run, ps))
    reply = run_cancel._handler({"run_id": RUN}, repo_root=tmp_path)
    assert reply["released"] is False and reply["fire_still_running"] == [DRIVER]
    assert DRIVER not in reply["stopped"] and RUN in idx.read(tmp_path, 1000)


def test_argv_mismatch_is_refused_and_nothing_is_touched(tmp_path):
    path, record = _fire(tmp_path)
    table = _table(path)
    table[DRIVER]["argv"] = _argv(tmp_path / "other.json")
    ps = FakePs(table)
    out = fire_stop.stop_fire(path, record, ps)
    assert "argv mismatch" in out["skipped"] and ps.ended == [] and out["survivors"] == []


def test_reused_pid_with_other_creation_time_is_refused(tmp_path):
    path, record = _fire(tmp_path)
    table = _table(path)
    table[DRIVER]["created"] = BORN + 3600
    ps = FakePs(table)
    out = fire_stop.stop_fire(path, record, ps)
    assert "creation time" in out["skipped"] and ps.ended == []


def test_record_without_pid_birth_is_not_trusted(tmp_path):
    path, record = _fire(tmp_path, pid_birth=None)
    ps = FakePs(_table(path))
    assert fire_stop.stop_fire(path, record, ps)["stopped"] == [] and ps.ended == []


def test_dead_driver_is_skipped(tmp_path):
    path, record = _fire(tmp_path)
    out = fire_stop.stop_fire(path, record, FakePs({}))
    assert out["skipped"] == "driver not running" and out["survivors"] == []


def test_surviving_process_is_reported(tmp_path):
    path, record = _fire(tmp_path)
    ps = FakePs(_table(path), immortal={301})
    out = fire_stop.stop_fire(path, record, ps)
    assert 301 in out["survivors"] and 301 not in out["stopped"]


def test_cancel_replies_released_false_when_a_process_survives(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    assert idx.claim(tmp_path, RUN, ["a.py"]) == {}
    path, _ = _fire(tmp_path)
    ps = FakePs(_table(path), immortal={201})
    real = fire_stop.stop_run_fires
    monkeypatch.setattr(fire_stop, "stop_run_fires", lambda r, run: real(r, run, ps))
    reply = run_cancel._handler({"run_id": RUN}, repo_root=tmp_path)
    assert reply["released"] is False and reply["fire_still_running"] == [201]
    assert RUN in idx.read(tmp_path, 1000)
    assert json.loads((run_dir / hr.HALTED_FILE).read_text(encoding="utf-8"))["halted"] == "cancelled"


def test_cancel_reports_stopped_pids_and_releases(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    assert idx.claim(tmp_path, RUN, ["a.py"]) == {}
    path, _ = _fire(tmp_path)
    ps = FakePs(_table(path))
    real = fire_stop.stop_run_fires
    monkeypatch.setattr(fire_stop, "stop_run_fires", lambda r, run: real(r, run, ps))
    reply = run_cancel._handler({"run_id": RUN}, repo_root=tmp_path)
    assert reply["released"] is True and reply["stopped"] == [DRIVER, 201, 301, 401]
    assert "fire_still_running" not in reply and RUN not in idx.read(tmp_path, 1000)
