"""Supervisor stop-request channel: a request addressed to (pid, procStart) is honoured by that process only."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roster = _load("unimatrix-roster.py", "handover_roster")
REMOTE = "example-operator@machine-a.local"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    (tmp_path / "unimatrix").mkdir()
    return tmp_path / "unimatrix"


def test_stop_request_path_is_per_pid_under_the_settings_home(home):
    assert roster.stop_request_path(77) == home / "stop-77.json"


def test_stop_request_carries_identity_and_is_written_atomically(home):
    roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)
    body = json.loads((home / f"stop-{os.getpid()}.json").read_text())
    assert {k: body[k] for k in ("pid", "procStart", "role", "remote")} == {
        "pid": os.getpid(), "procStart": "start-A", "role": "bridge", "remote": REMOTE}
    assert isinstance(body["requestedAt"], float)
    assert not list(home.glob("*.tmp"))


def test_stop_request_is_seen_by_the_addressed_process_only(home):
    roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)
    assert roster.stop_requested("start-A") is True
    # A replacement reusing the pid has a different procStart: it never reads the request as its own.
    assert roster.stop_requested("start-B") is False


def test_stop_request_for_another_pid_is_ignored(home):
    roster.request_stop(os.getpid() + 1, "start-A", "bridge", REMOTE)
    assert roster.stop_requested("start-A") is False


def test_no_request_or_corrupt_request_is_not_a_stop(home):
    assert roster.stop_requested("start-A") is False
    (home / f"stop-{os.getpid()}.json").write_text("{not json")
    assert roster.stop_requested("start-A") is False
    (home / f"stop-{os.getpid()}.json").write_text("[]")
    assert roster.stop_requested("start-A") is False


def test_clear_stop_removes_request_and_stamp_and_tolerates_absence(home):
    pid = os.getpid()
    roster.request_stop(pid, "start-A", "inbox", REMOTE)
    (home / f"supervisor-{pid}.json").write_text("{}")
    roster.clear_stop(pid)
    assert not list(home.iterdir())
    roster.clear_stop(pid)


def test_stamp_records_role_only_when_given(home, monkeypatch):
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: f"start-{pid}")
    stamp = home / f"supervisor-{os.getpid()}.json"
    roster.stamp_supervisor(REMOTE)
    assert "role" not in json.loads(stamp.read_text())
    roster.stamp_supervisor(REMOTE, "bridge")
    assert json.loads(stamp.read_text())["role"] == "bridge"


def test_wait_or_stop_sleeps_in_slices_and_returns_true_on_request(home):
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)

    assert roster.wait_or_stop(30, "start-A", sleep=sleep) is True
    assert sleeps == [roster.STOP_POLL_SECONDS] * 3


def test_wait_or_stop_runs_the_full_wait_without_a_request(home):
    sleeps = []
    assert roster.wait_or_stop(2, "start-A", sleep=sleeps.append) is False
    assert sleeps == [roster.STOP_POLL_SECONDS] * 4


def _stub_main_deps(monkeypatch, bridges_killed):
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: f"start-{pid}")
    monkeypatch.setattr(roster, "reap_orphaned_mirrors", lambda *a, **k: None)
    monkeypatch.setattr(roster, "link_probe", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(roster, "note_link", lambda link, *a, **k: link)
    monkeypatch.setattr(roster, "fetch_roster", lambda *a, **k: [])
    monkeypatch.setattr(roster, "far_rows", lambda *a, **k: [])
    monkeypatch.setattr(roster, "sync", lambda *a, **k: None)
    monkeypatch.setattr(roster, "reap_unbacked_agy_faces", lambda *a, **k: None)
    monkeypatch.setattr(roster, "tree_kill_many", lambda pids: bridges_killed.append(list(pids)))


def _argv(tmp_path):
    return [str(tmp_path / "roster.log"), "--remote", REMOTE, "--key", str(tmp_path / "key")]


def test_stop_request_main_returns_at_loop_top_and_clears_the_request(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    roster.request_stop(os.getpid(), f"start-{os.getpid()}", "bridge", REMOTE)
    monkeypatch.setattr(roster.time, "sleep", lambda s: pytest.fail("loop slept after a pending stop"))
    assert roster.main(_argv(tmp_path)) is None
    assert "STOPPED-BY-REQUEST" in (tmp_path / "roster.log").read_text()
    assert not (home / f"stop-{os.getpid()}.json").exists()
    assert not (home / f"supervisor-{os.getpid()}.json").exists()
    assert killed == [[]]


def test_stop_request_main_returns_within_one_slice_of_a_request_made_mid_poll(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            roster.request_stop(os.getpid(), f"start-{os.getpid()}", "bridge", REMOTE)

    monkeypatch.setattr(roster.time, "sleep", sleep)
    roster.main(_argv(tmp_path))
    assert sleeps == [roster.STOP_POLL_SECONDS] * 2
    assert "STOPPED-BY-REQUEST" in (tmp_path / "roster.log").read_text()
    assert killed == [[]]


def test_stop_request_for_a_previous_incarnation_does_not_stop_main(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    roster.request_stop(os.getpid(), "start-of-a-dead-process", "bridge", REMOTE)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(roster.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        roster.main(_argv(tmp_path))
    assert (home / f"stop-{os.getpid()}.json").exists()
    assert killed == [[]]
