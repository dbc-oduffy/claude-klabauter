"""Behavior pins for the spawn-free unimatrix liveness module."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
SRC = BIN / "unimatrix-liveness.py"


def _load() -> "ModuleType":
    spec = importlib.util.spec_from_file_location("unimatrix_liveness", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _load()


def test_module_does_not_import_subprocess() -> None:
    src = SRC.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import subprocess|from subprocess)", src, re.M)
    assert "subprocess" not in lv.__dict__


def _harness_start(pid: int) -> str:
    """The procStart the harness records: raw /proc starttime ticks on Linux, `ps -o lstart=` text on macOS."""
    if sys.platform == "linux":
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return str(int(stat[stat.rindex(")") + 2:].split()[19]))
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX proc_start")
def test_proc_start_matches_the_harness_format_for_own_pid() -> None:
    assert lv.proc_start(os.getpid()) == _harness_start(os.getpid())


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX proc_start")
def test_proc_start_matches_the_harness_format_for_child_pid() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert lv.proc_start(child.pid) == _harness_start(child.pid)
    finally:
        child.kill()
        child.wait()


def _fake_files(monkeypatch: pytest.MonkeyPatch, files: dict) -> None:
    import builtins
    real = builtins.open

    def fake(path, *a, **kw):
        key = str(path).replace("\\", "/")
        if key in files:
            import io
            return io.StringIO(files[key])
        if key.startswith(("/proc/", "/etc/", "/var/lib/")):
            raise FileNotFoundError(key)
        return real(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake)


def test_linux_start_is_the_raw_starttime_ticks_even_for_a_spaced_comm(monkeypatch: pytest.MonkeyPatch) -> None:
    tail = " ".join(["S", "1"] + ["0"] * 17 + ["987654", "0"])  # fields 3.. ; starttime is the 20th after the comm
    _fake_files(monkeypatch, {"/proc/42/stat": f"42 (a) b) {tail}\n"})
    assert lv._linux_start(42) == "987654"


def test_linux_pid_domain_joins_machine_id_and_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_files(monkeypatch, {"/etc/machine-id": "abc123\n"})
    monkeypatch.setattr(os, "readlink", lambda p: "pid:[4026531836]")
    assert lv.linux_pid_domain() == "linux:abc123:pid:[4026531836]"


def test_linux_pid_domain_falls_back_to_the_dbus_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_files(monkeypatch, {"/var/lib/dbus/machine-id": "dbus99\n"})
    monkeypatch.setattr(os, "readlink", lambda p: "pid:[1]")
    assert lv.linux_pid_domain() == "linux:dbus99:pid:[1]"


@pytest.mark.parametrize("have_id, have_ns", [(False, True), (True, False)])
def test_linux_pid_domain_is_bare_linux_when_a_part_is_missing(monkeypatch: pytest.MonkeyPatch, have_id, have_ns) -> None:
    _fake_files(monkeypatch, {"/etc/machine-id": "abc\n"} if have_id else {})

    def readlink(p):
        if not have_ns:
            raise OSError("no ns")
        return "pid:[1]"

    monkeypatch.setattr(os, "readlink", readlink)
    assert lv.linux_pid_domain() == "linux"


def test_lstart_pads_one_digit_day() -> None:
    epoch = time.mktime((2026, 11, 4, 1, 19, 29, 0, 0, -1))
    assert lv._lstart(epoch) == "Wed Nov  4 01:19:29 2026"


def _rec(pid=None):
    pid = pid or os.getpid()
    return {"pid": pid, "procStart": lv.proc_start(pid)}


def test_supervisor_live_true_false_cases() -> None:
    assert lv.supervisor_live(_rec())
    assert not lv.supervisor_live({"pid": os.getpid(), "procStart": "Mon Jan  1 00:00:00 2001"})
    assert not lv.supervisor_live({"pid": "x"})
    assert not lv.supervisor_live({})
    assert not lv.supervisor_live({"pid": True, "procStart": "x"})
    assert not lv.pid_alive(0) and not lv.pid_alive(-1)


def test_entry_live_matching_procstart_is_live() -> None:
    assert lv.entry_live(_rec())


def test_entry_live_different_procstart_is_not_live() -> None:
    assert not lv.entry_live({"pid": os.getpid(), "procStart": "Mon Jan  1 00:00:00 2001"})


def test_entry_live_without_procstart_falls_back_to_pid_alive() -> None:
    assert lv.entry_live({"pid": os.getpid()})
    assert lv.entry_live({"pid": os.getpid(), "procStart": ""})
    assert lv.entry_live({"pid": os.getpid(), "procStart": 5})


def test_entry_live_dead_or_malformed_pid_is_not_live() -> None:
    assert not lv.entry_live({"pid": 0, "procStart": "x"})
    assert not lv.entry_live({"pid": True})
    assert not lv.entry_live({"pid": "x"})
    assert not lv.entry_live({})
    assert not lv.entry_live(None)


def test_entry_live_proc_start_oserror_is_not_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(pid, platform=None):
        raise OSError("unreadable")
    monkeypatch.setattr(lv, "proc_start", boom)
    assert lv.entry_live({"pid": os.getpid(), "procStart": "anything"})
    monkeypatch.setattr(lv, "pid_alive", lambda pid: False)
    assert not lv.entry_live({"pid": os.getpid(), "procStart": "anything"})


def test_stamp_fresh_accepts_only_a_recent_integer_stamp() -> None:
    now = 1_000_000.0
    ms = int(now * 1000)
    assert lv.stamp_fresh({"s": ms - 5000}, "s", 10, now=now) is True
    assert lv.stamp_fresh({"s": ms - 11000}, "s", 10, now=now) is False
    assert lv.stamp_fresh({"s": ms + 1000}, "s", 10, now=now) is False
    for bad in ({}, {"s": "1"}, {"s": True}, {"s": None}, None):
        assert lv.stamp_fresh(bad, "s", 10, now=now) is False


def _agy_home(tmp_path, agy_pid):
    base = tmp_path / "unimatrix"
    (base / "agy-legs").mkdir(parents=True)
    (base / "peers.json").write_text(json.dumps([{"name": "agy-x-on-box", "vendor": "agy", "target": "conv-1"}]))
    if agy_pid is not None:
        (base / "agy-legs" / "conv-1.json").write_text(json.dumps({"pid": 1, "agy_pid": agy_pid}))
    return tmp_path


def test_agy_face_unbacked_when_its_agy_process_is_gone(tmp_path: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    assert lv.agy_face_unbacked(face, _agy_home(tmp_path, 2_000_000_000)) is True


def test_agy_face_backed_while_its_agy_process_lives(tmp_path: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    assert lv.agy_face_unbacked(face, _agy_home(tmp_path, os.getpid())) is False


def test_agy_face_without_a_leg_record_is_unbacked_and_a_non_face_never_is(tmp_path: Path) -> None:
    home = _agy_home(tmp_path, None)
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}, home) is True
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy"}, home) is False
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "codex", "unimatrixRouterPid": 7}, home) is False


def test_agy_face_with_no_peers_row_is_unbacked_but_an_unreadable_peers_file_is_not(tmp_path: Path) -> None:
    home = _agy_home(tmp_path, os.getpid())
    assert lv.agy_face_unbacked({"name": "agy-Temp-bafde89c04-on-machine-a", "vendor": "agy",
                                 "unimatrixRouterPid": 7}, home) is True
    (tmp_path / "unimatrix" / "peers.json").write_text("{not json")
    assert lv.agy_face_unbacked({"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}, home) is False


@pytest.fixture(autouse=True)
def _agy_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point agy's brain root under tmp so presence locks never read the real home."""
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "agy" / "brain"))
    presence = tmp_path / "agy" / "presence"
    presence.mkdir(parents=True)
    return presence


class _Held:
    """Holds the presence lock for a conversation through the transport probe, as agy does."""

    def __init__(self, presence: Path, conversation: str) -> None:
        (presence / f"{conversation}.lock").touch()
        self.file = open(presence / f"{conversation}.lock", "r+b")
        self.transport = lv._transport()
        assert self.transport.lock_file(self.file) is True

    def release(self) -> None:
        self.transport.unlock_file(self.file)
        self.file.close()


def _write_leg(home: Path, conversation: str, rec: dict) -> None:
    legs = home / "unimatrix" / "agy-legs"
    legs.mkdir(parents=True, exist_ok=True)
    (legs / f"{conversation}.json").write_text(json.dumps(rec))


def test_agy_presence_held_follows_the_lock(_agy_brain: Path) -> None:
    assert lv.agy_presence_held("conv-1") is False
    held = _Held(_agy_brain, "conv-1")
    try:
        assert lv.agy_presence_held("conv-1") is True
        assert lv.agy_presence_held("conv-1", presence_dir=_agy_brain) is True
        assert lv.agy_presence_held("conv-2") is False
    finally:
        held.release()
    assert lv.agy_presence_held("conv-1") is False


def test_agy_presence_held_refuses_unsafe_names(_agy_brain: Path) -> None:
    for bad in ("../conv-1", "a/b", "a\\b", "", None):
        assert lv.agy_presence_held(bad) is False


def test_agy_session_state_live_when_presence_lock_held(tmp_path: Path, _agy_brain: Path) -> None:
    held = _Held(_agy_brain, "conv-1")
    try:
        rec = {"agy_pid": 2_000_000_000, "startedAt": 1}
        assert lv.agy_session_state("conv-1", rec, tmp_path) == "live"
        assert lv.agy_session_state("conv-1", {"agy_pid": None}, tmp_path, now=10**9) == "live"
    finally:
        held.release()


def test_agy_session_state_dead_pid_without_lock_is_dead(tmp_path: Path) -> None:
    assert lv.agy_session_state("conv-1", {"agy_pid": 2_000_000_000}, tmp_path) == "dead"


def test_agy_session_state_live_pid_without_lock_is_live(tmp_path: Path) -> None:
    assert lv.agy_session_state("conv-1", {"agy_pid": os.getpid()}, tmp_path) == "live"


def test_agy_session_state_reused_pid_is_dead(tmp_path: Path) -> None:
    rec = {"agy_pid": os.getpid(), "agy_proc_start": "Mon Jan  1 00:00:00 2001"}
    assert lv.agy_session_state("conv-1", rec, tmp_path) == "dead"
    rec["agy_proc_start"] = lv.proc_start(os.getpid())
    assert lv.agy_session_state("conv-1", rec, tmp_path) == "live"


def test_agy_session_state_no_pid_is_unknown_inside_grace_and_dead_past_it(tmp_path: Path) -> None:
    now = time.time()
    fresh = {"agy_pid": None, "startedAt": int((now - 5) * 1000)}
    old = {"agy_pid": None, "startedAt": int((now - lv.AGY_SESSION_GRACE_SECONDS - 5) * 1000)}
    assert lv.agy_session_state("conv-1", fresh, tmp_path, now=now) == "unknown"
    assert lv.agy_session_state("conv-1", old, tmp_path, now=now) == "dead"
    assert lv.agy_session_state("conv-1", {"ts": now - lv.AGY_SESSION_GRACE_SECONDS - 5}, tmp_path, now=now) == "dead"


def test_agy_session_state_ages_a_stampless_record_by_its_file_mtime(tmp_path: Path) -> None:
    _write_leg(tmp_path, "conv-1", {"pid": 1, "agy_pid": None})
    path = tmp_path / "unimatrix" / "agy-legs" / "conv-1.json"
    now = time.time()
    os.utime(path, (now, now))
    assert lv.agy_session_state("conv-1", {"agy_pid": None}, tmp_path, now=now) == "unknown"
    os.utime(path, (now - lv.AGY_SESSION_GRACE_SECONDS - 5,) * 2)
    assert lv.agy_session_state("conv-1", {"agy_pid": None}, tmp_path, now=now) == "dead"


def test_agy_session_state_unprovable_age_is_unknown(tmp_path: Path) -> None:
    assert lv.agy_session_state("conv-1", {"agy_pid": None}, tmp_path) == "unknown"
    assert lv.agy_session_state("a/b", {"agy_pid": None, "ts": 1}, tmp_path) == "dead"


def test_agy_face_with_a_dead_or_absent_pid_and_no_lock_is_unbacked_past_grace(tmp_path: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    old = int(time.time() - lv.AGY_SESSION_GRACE_SECONDS - 30) * 1000
    home = _agy_home(tmp_path, None)
    _write_leg(home, "conv-1", {"pid": 2_000_000_000, "agy_pid": None, "startedAt": old})
    assert lv.agy_face_unbacked(face, home) is True


def test_agy_face_with_no_pid_is_backed_inside_grace(tmp_path: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    home = _agy_home(tmp_path, None)
    _write_leg(home, "conv-1", {"pid": 1, "agy_pid": None, "startedAt": int(time.time() * 1000)})
    assert lv.agy_face_unbacked(face, home) is False


def test_agy_face_with_a_held_presence_lock_is_backed_despite_a_dead_pid(tmp_path: Path, _agy_brain: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    home = _agy_home(tmp_path, 2_000_000_000)
    held = _Held(_agy_brain, "conv-1")
    try:
        assert lv.agy_face_unbacked(face, home) is False
    finally:
        held.release()
    assert lv.agy_face_unbacked(face, home) is True


def test_agy_face_with_a_pending_leg_is_unbacked_but_live_or_absent_mode_is_backed(tmp_path: Path) -> None:
    face = {"name": "agy-x-on-box", "vendor": "agy", "unimatrixRouterPid": 7}
    home = _agy_home(tmp_path, None)
    _write_leg(home, "conv-1", {"pid": 1, "agy_pid": os.getpid(), "mode": "pending"})
    assert lv.agy_face_unbacked(face, home) is True
    _write_leg(home, "conv-1", {"pid": 1, "agy_pid": os.getpid(), "mode": "live"})
    assert lv.agy_face_unbacked(face, home) is False
    _write_leg(home, "conv-1", {"pid": 1, "agy_pid": os.getpid()})
    assert lv.agy_face_unbacked(face, home) is False


@pytest.mark.parametrize("mirror_field", [
    {"unimatrixRemote": "example-operator@machine-b"},
    {"macSocketPath": "/tmp/cc-socks/cc-msg-far.sock"},
    {"unimatrixMirror": True},
])
def test_agy_face_unbacked_ignores_remote_mirrors(tmp_path: Path, mirror_field: dict) -> None:
    home = _agy_home(tmp_path, 2_000_000_000)
    row = {"name": "agy-remote-on-machine-b.machine-b-lan", "vendor": "agy",
           "unimatrixRouterPid": 7, **mirror_field}
    assert lv.agy_face_unbacked(row, home) is False


def _peer_home(tmp_path, rows, legs=()):
    base = tmp_path / "unimatrix"
    (base / "agy-legs").mkdir(parents=True)
    (base / "peers.json").write_text(json.dumps(rows))
    for leg in legs:
        (base / "agy-legs" / f"{leg}.json").write_text(json.dumps({"agy_pid": 1}))
    return tmp_path


def test_agy_peer_rows_orphaned_lists_only_agy_rows_without_a_leg_record(tmp_path: Path) -> None:
    home = _peer_home(tmp_path, [
        {"name": "a-dead", "vendor": "agy", "target": "dead-1"},
        {"name": "a-live", "vendor": "agy", "target": "live-1"},
        {"name": "c-row", "vendor": "codex", "target": "codex-1"},
        {"name": "a-unsafe", "vendor": "agy", "target": "../x"},
        {"name": "a-unsafe2", "vendor": "agy", "target": "a\\b"},
        {"name": "a-empty", "vendor": "agy", "target": ""},
        {"name": "a-notarget", "vendor": "agy"},
        "junk",
    ], legs=["live-1"])
    assert lv.agy_peer_rows_orphaned(home) == [("a-dead", "dead-1")]


def test_agy_peer_rows_orphaned_reads_the_peers_container_shape(tmp_path: Path) -> None:
    home = _peer_home(tmp_path, {"peers": [{"name": "a-dead", "vendor": "agy", "target": "dead-1"}]})
    assert lv.agy_peer_rows_orphaned(home) == [("a-dead", "dead-1")]


def test_agy_peer_rows_orphaned_unreadable_or_missing_peers_file_is_empty(tmp_path: Path) -> None:
    home = _peer_home(tmp_path, [])
    (tmp_path / "unimatrix" / "peers.json").write_text("{not json")
    assert lv.agy_peer_rows_orphaned(home) == []
    assert lv.agy_peer_rows_orphaned(tmp_path / "nowhere") == []


def test_codex_holder_live_matching_start_is_live() -> None:
    me = _rec()
    assert lv.codex_holder_live({"codexPid": me["pid"], "codexProcStart": me["procStart"]}) is True


def test_codex_holder_live_dead_pid_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv, "pid_alive", lambda pid: False)
    assert lv.codex_holder_live({"codexPid": 4242, "codexProcStart": "x"}) is False


def test_codex_holder_live_checks_current_writer_after_original_holder_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(lv, "pid_alive", lambda _pid: False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    lock_dir = tmp_path / "thread-writer-locks"
    monkeypatch.setattr(
        lv, "agy_presence_held",
        lambda target, *, presence_dir: target == "resumed-thread" and presence_dir == lock_dir,
    )
    monkeypatch.setattr(lv, "lock_holder_pids", lambda path: [777] if path == lock_dir / "resumed-thread.lock" else [])
    monkeypatch.setattr(lv, "codex_cli_process", lambda pid: pid == 777)
    row = {"vendor": "codex", "codexPid": 4242, "codexProcStart": "x"}
    assert lv.codex_holder_live({**row, "target": "resumed-thread"}) is True
    assert lv.codex_holder_live({**row, "target": "closed-thread"}) is False


def _locked_dead_holder_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, holders) -> dict:
    monkeypatch.setattr(lv, "pid_alive", lambda _pid: False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(lv, "agy_presence_held", lambda target, *, presence_dir: True)
    monkeypatch.setattr(lv, "lock_holder_pids", lambda path: holders)
    return {"vendor": "codex", "codexPid": 4242, "codexProcStart": "x", "target": "t-1"}


def test_codex_holder_live_app_server_lock_holder_is_not_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = _locked_dead_holder_row(monkeypatch, tmp_path, [2846, 19008])
    monkeypatch.setattr(lv, "codex_cli_process", lambda pid: False)
    assert lv.codex_holder_live(row) is False


def test_codex_holder_live_one_cli_among_lock_holders_is_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = _locked_dead_holder_row(monkeypatch, tmp_path, [2846, 31000])
    monkeypatch.setattr(lv, "codex_cli_process", lambda pid: pid == 31000)
    assert lv.codex_holder_live(row) is True


@pytest.mark.parametrize("holders", [None, []])
def test_codex_holder_live_unidentifiable_lock_holder_is_not_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, holders,
) -> None:
    row = _locked_dead_holder_row(monkeypatch, tmp_path, holders)
    monkeypatch.setattr(lv, "codex_cli_process", lambda pid: True)
    assert lv.codex_holder_live(row) is False


def test_codex_holder_live_dead_pid_branch_spawns_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def no_spawn(*_a, **_k):
        raise AssertionError("spawned a process")
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(lv, "pid_alive", lambda _pid: False)
    row = {"vendor": "codex", "codexPid": 4242, "codexProcStart": "x", "target": "t-1"}
    assert lv.codex_holder_live(row) is False
    (tmp_path / "thread-writer-locks").mkdir()
    (tmp_path / "thread-writer-locks" / "t-1.lock").write_bytes(b"")
    assert lv.codex_holder_live(row) is False


@pytest.mark.parametrize("argv,expected", [
    (["codex"], True),
    (["C:\\bin\\codex.exe", "resume", "t-1"], True),
    (["/usr/bin/node", "/lib/node_modules/@openai/codex/bin/codex.js"], True),
    (["codex", "app-server", "--listen", "stdio://"], False),
    (["node", "/lib/@openai/codex/bin/codex.js", "app-server"], False),
    (["node", "server.js"], False),
    (["python", "codex"], False),
    ([], False),
    (None, False),
])
def test_argv_is_codex_cli_admits_only_cli_images(argv, expected) -> None:
    assert lv._proc()._argv_is_codex_cli(argv) is expected


def test_codex_cli_process_unreadable_argv_is_not_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lv._proc(), "_process_argv", lambda pid: None)
    assert lv._proc().codex_cli_process(1) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Restart Manager lists any process; /proc and libproc scans filter to codex/node")
def test_lock_holder_pids_sees_this_process_holding_the_file(tmp_path: Path) -> None:
    lock = tmp_path / "held.lock"
    with open(lock, "wb"):
        assert os.getpid() in lv._proc().lock_holder_pids(lock)
    assert os.getpid() not in lv._proc().lock_holder_pids(lock)


def test_codex_holder_live_differing_start_is_false() -> None:
    row = {"codexPid": os.getpid(), "codexProcStart": "Mon Jan  1 00:00:00 2001"}
    assert lv.codex_holder_live(row) is False


def test_codex_holder_live_no_holder_is_none() -> None:
    assert lv.codex_holder_live({}) is None
    assert lv.codex_holder_live({"codexPid": True}) is None
    assert lv.codex_holder_live({"codexPid": "7"}) is None
    assert lv.codex_holder_live(None) is None


def test_codex_holder_live_unreadable_start_is_not_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(pid, platform=None):
        raise OSError("unreadable")
    monkeypatch.setattr(lv, "proc_start", boom)
    assert lv.codex_holder_live({"codexPid": os.getpid(), "codexProcStart": "x"}) is True


def _setup(tmp_path, aliases, live: bool = True, age: int = 0):
    sdir = tmp_path / "unimatrix"
    sdir.mkdir()
    cache = sdir / "autolink-boxes.json"
    cache.write_text(json.dumps({"aliases": aliases}))
    if age:
        t = time.time() - age
        os.utime(cache, (t, t))
    for a in aliases:
        rec = {"bridge": _rec(), "inbox": _rec()} if live else {"bridge": _rec(), "inbox": {"pid": 0}}
        (sdir / f"autolink-{lv._slug(a)}.json").write_text(json.dumps(rec))
    return sdir


def test_healthy_true_when_all_live(tmp_path: Path) -> None:
    assert lv.healthy(_setup(tmp_path, ["a@h", "b"]))


def test_healthy_true_for_fresh_empty_cache(tmp_path: Path) -> None:
    assert lv.healthy(_setup(tmp_path, []))


def test_healthy_false_when_one_role_dead(tmp_path: Path) -> None:
    assert not lv.healthy(_setup(tmp_path, ["a"], live=False))


def test_healthy_false_when_record_missing(tmp_path: Path) -> None:
    sdir = _setup(tmp_path, ["a"])
    (sdir / "autolink-a.json").unlink()
    assert not lv.healthy(sdir)


def test_healthy_false_without_cache(tmp_path: Path) -> None:
    (tmp_path / "unimatrix").mkdir()
    assert not lv.healthy(tmp_path / "unimatrix")
    assert not lv.healthy(tmp_path / "absent")


def test_healthy_false_for_stale_cache_over_24h(tmp_path: Path) -> None:
    assert not lv.healthy(_setup(tmp_path, ["a"], age=24 * 3600 + 5))


def test_healthy_true_just_under_24h(tmp_path: Path) -> None:
    assert lv.healthy(_setup(tmp_path, ["a"], age=24 * 3600 - 60))


def test_healthy_honors_now(tmp_path: Path) -> None:
    sdir = _setup(tmp_path, ["a"])
    assert not lv.healthy(sdir, now=time.time() + 25 * 3600)


def test_healthy_probe_answered_for_every_alias_is_healthy(tmp_path: Path) -> None:
    seen = []
    assert lv.healthy(_setup(tmp_path, ["a@h", "b"]), probe=lambda a: seen.append(a))
    assert seen == ["a@h", "b"]


def test_healthy_probe_reason_on_any_alias_is_unhealthy(tmp_path: Path) -> None:
    sdir = _setup(tmp_path, ["a@h", "b"])
    assert not lv.healthy(sdir, probe=lambda a: "no echo" if a == "b" else None)


def test_healthy_probe_reaches_every_dead_alias(tmp_path: Path) -> None:
    seen = []

    def probe(a) -> str:
        seen.append(a)
        return "no echo"

    assert not lv.healthy(_setup(tmp_path, ["a@h", "b"]), probe=probe)
    assert sorted(seen) == ["a@h", "b"]


def test_healthy_probes_run_concurrently(tmp_path: Path) -> None:
    import threading

    barrier = threading.Barrier(2, timeout=5)

    def probe(a) -> None:
        barrier.wait()
        return None

    assert lv.healthy(_setup(tmp_path, ["a@h", "b"]), probe=probe)


def test_healthy_probe_not_called_when_a_local_supervisor_is_dead(tmp_path: Path) -> None:
    calls = []
    assert not lv.healthy(_setup(tmp_path, ["a"], live=False), probe=lambda a: calls.append(a))
    assert calls == []


def test_healthy_probe_not_called_for_stale_cache(tmp_path: Path) -> None:
    calls = []
    assert not lv.healthy(_setup(tmp_path, ["a"], age=24 * 3600 + 5), probe=lambda a: calls.append(a))
    assert calls == []


def test_healthy_without_probe_is_unchanged_for_live_records(tmp_path: Path) -> None:
    assert lv.healthy(_setup(tmp_path, ["a"]), probe=None)


def test_healthy_false_for_malformed_cache(tmp_path: Path) -> None:
    sdir = _setup(tmp_path, [])
    (sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": "a"}))
    assert not lv.healthy(sdir)


def test_backoff_doubles_caps_and_clears(tmp_path: Path) -> None:
    assert lv.backoff_until(tmp_path, "a") == 0.0
    expect = [60, 120, 240, 480, 960, 1920, 3600, 3600]
    for n, delay in enumerate(expect):
        lv.note_probe(tmp_path, "a", False, now=1000.0)
        assert lv.backoff_until(tmp_path, "a") == 1000.0 + delay, n
    assert json.loads((tmp_path / "autolink-a.backoff.json").read_text())["failures"] == len(expect)
    lv.note_probe(tmp_path, "a", True)
    assert lv.backoff_until(tmp_path, "a") == 0.0
    assert not (tmp_path / "autolink-a.backoff.json").exists()
    lv.note_probe(tmp_path, "a", True)


def test_backoff_is_per_alias_and_slugged(tmp_path: Path) -> None:
    lv.note_probe(tmp_path, "u@h", False, now=0.0)
    assert (tmp_path / "autolink-u_at_h.backoff.json").exists()
    assert lv.backoff_until(tmp_path, "other") == 0.0


def test_state_dir_derives_from_settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert lv.state_dir() == tmp_path / "unimatrix"


def _logs_db(home, name, rows) -> None:
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(home / name)) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                     "ts_nanos INTEGER NOT NULL, thread_id TEXT, process_uuid TEXT)")
        conn.executemany("INSERT INTO logs (ts, ts_nanos, thread_id, process_uuid) VALUES (?,?,?,?)", rows)
        conn.commit()


def test_codex_thread_holder_takes_newest_process_across_dbs(tmp_path: Path) -> None:
    me = os.getpid()
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "t1", "pid:999999:old"), (5, 0, "t2", "pid:7:x")])
    _logs_db(tmp_path, "logs_2.sqlite", [(20, 0, "t1", f"pid:{me}:new"), (30, 0, "t1", None)])
    got = lv.codex_thread_holder(tmp_path, "t1")
    assert got["codexPid"] == me
    assert got["codexProcStart"] == lv.proc_start(me)
    assert lv.codex_holder_live(got) is True


def test_codex_thread_holder_unknown_thread_or_no_db_is_empty(tmp_path: Path) -> None:
    assert lv.codex_thread_holder(tmp_path, "t1") == {}
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "other", "pid:5:x"), (11, 0, "t1", "garbage")])
    assert lv.codex_thread_holder(tmp_path, "t1") == {}
    assert lv.codex_thread_holder(tmp_path, "missing") == {}


def test_codex_thread_holder_dead_pid_reads_dead(tmp_path: Path) -> None:
    _logs_db(tmp_path, "logs_1.sqlite", [(10, 0, "t1", "pid:2147480000:x")])
    got = lv.codex_thread_holder(tmp_path, "t1")
    assert got == {"codexPid": 2147480000}
    assert lv.codex_holder_live(got) is False


def _codex_home(tmp_path, **holder):
    base = tmp_path / "unimatrix"
    base.mkdir(parents=True, exist_ok=True)
    (base / "peers.json").write_text(json.dumps([{"name": "codex-x", "vendor": "codex", "target": "t-1", **holder}]))
    return tmp_path


def _codex_face(started_ago_seconds: int = 5000):
    return {"name": "codex-x", "vendor": "codex", "sessionId": "peer-codex-x-9", "target": "t-1",
            "startedAt": int((time.time() - started_ago_seconds) * 1000)}


def test_codex_face_unbacked_when_its_holder_is_dead_and_backed_while_it_lives(tmp_path: Path) -> None:
    assert lv.codex_face_unbacked(_codex_face(), _codex_home(tmp_path, codexPid=2_000_000_000)) is True
    assert lv.codex_face_unbacked(_codex_face(), _codex_home(tmp_path, codexPid=os.getpid())) is False


def test_codex_face_with_no_recorded_holder_is_unbacked_only_past_its_grace(tmp_path: Path) -> None:
    home = _codex_home(tmp_path)
    assert lv.codex_face_unbacked(_codex_face(5000), home) is True
    assert lv.codex_face_unbacked(_codex_face(5), home) is False


def test_codex_face_check_never_claims_what_it_cannot_prove(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, codexPid=2_000_000_000)
    assert lv.codex_face_unbacked(dict(_codex_face(), target="t-other", name="other"), home) is False
    assert lv.codex_face_unbacked(dict(_codex_face(), unimatrixRemote="me@box"), home) is False
    assert lv.codex_face_unbacked(dict(_codex_face(), vendor="claude"), home) is False
    assert lv.codex_face_unbacked(_codex_face(), tmp_path / "no-peers-file") is False


def test_a_managed_app_server_daemon_that_is_the_recorded_holder_and_holds_the_thread_lock_backs_the_face() -> None:
    app = ["/Users/x/.codex/packages/app-server-daemon/bin/codex", "app-server", "--listen", "unix://"]
    row = {"vendor": "codex", "target": "t1", "codexPid": 2624}
    kw = dict(pid_alive_fn=lambda p: True, proc_start_fn=lambda p: None, presence_held_fn=lambda *a, **k: True,
              lock_pids_fn=lambda p: [2624], cli_fn=lambda p: False, argv_fn=lambda p: app)
    pm = lv._proc()
    assert pm.codex_holder_live(row, **kw) is True
    assert pm.codex_holder_live(row, **{**kw, "lock_pids_fn": lambda p: [999]}) is False
    assert pm.codex_holder_live({**row, "codexPid": None}, **kw) is False


def _far_codex(**extra):
    return {"vendor": "codex", "macSocketPath": "/tmp/f.sock", "unimatrixRemote": "me@box",
            "startedAt": int(time.time() * 1000) - 2 * lv.CODEX_FACE_GRACE_SECONDS * 1000, **extra}


@pytest.mark.parametrize("extra", [{}, {"codexHolder": "unknown"}, {"codexHolder": "dead"}, {"codexHolder": 7}])
def test_far_codex_row_without_a_live_holder_is_unbacked_past_grace(extra) -> None:
    assert lv.far_codex_row_unbacked(_far_codex(**extra)) is True


def test_far_codex_row_is_backed_while_live_fresh_unstamped_or_not_codex() -> None:
    assert lv.far_codex_row_unbacked(_far_codex(codexHolder="live")) is False
    assert lv.far_codex_row_unbacked(_far_codex(startedAt=int(time.time() * 1000))) is False
    assert lv.far_codex_row_unbacked({k: v for k, v in _far_codex().items() if k != "startedAt"}) is False
    assert lv.far_codex_row_unbacked(_far_codex(vendor="claude")) is False
    assert lv.far_codex_row_unbacked(None) is False
