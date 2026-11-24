"""Behavior pins for the unimatrix grok peer: registry discovery and the cwd-pinned headless resume."""
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import CompletedProcess

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_grok", BIN / "unimatrix-peer-grok.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

SID = "01a1c000-0000-7000-8000-00000000c0c0"


def registry(tmp_path, rows):
    (tmp_path / "active_sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    return tmp_path


def box():
    return pg._clean(pg._contract().box_id(socket.gethostname()))


@pytest.mark.parametrize("host", ["Machine-b.local", "machine-b", "Machine-b"])
def test_peer_name_suffix_is_one_box_across_hostname_spellings(monkeypatch: pytest.MonkeyPatch, host) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    assert pg.peer_name(SID, "/nowhere/myrepo") == "grok-myrepo-01a1c000-on-machine-b"


def test_live_entry_yields_named_grok_peer(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "pkg"
    sub.mkdir()
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": str(sub), "opened_at": "x"}])
    peers = pg.discover_peers(home, pid_alive=lambda pid: pid == 4242)
    assert peers == [{"name": f"grok-myrepo-01a1c000-on-{box()}", "vendor": "grok",
                      "target": SID, "cwd": str(sub)}]


def test_dead_pid_yields_no_peer(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": str(tmp_path)}])
    assert pg.discover_peers(home, pid_alive=lambda pid: False) == []


@pytest.mark.parametrize("content", ["not json", "{}", "[1, 'x', {\"session_id\": 3}]",
                                     '[{"session_id": "a", "cwd": "/x", "pid": "9"}]'])
def test_malformed_registry_yields_no_peers(tmp_path: Path, content) -> None:
    (tmp_path / "active_sessions.json").write_text(content, encoding="utf-8")
    assert pg.discover_peers(tmp_path, pid_alive=lambda pid: True) == []


def test_absent_registry_yields_no_peers(tmp_path: Path) -> None:
    assert pg.discover_peers(tmp_path, pid_alive=lambda pid: True) == []


def test_taken_name_and_target_are_skipped(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 1, "cwd": str(tmp_path)}])
    name = pg.peer_name(SID, str(tmp_path))
    assert pg.discover_peers(home, lambda p: True, taken_names={name}) == []
    assert pg.discover_peers(home, lambda p: True, taken_targets={SID}) == []


def test_grok_home_honours_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    assert pg.grok_home() == tmp_path


def test_argv_is_cwd_pinned_without_add_dir_or_fork(tmp_path: Path) -> None:
    argv = pg.resume_argv("grok", SID, "hello", str(tmp_path))
    assert argv[argv.index("--cwd") + 1] == str(tmp_path)
    assert argv[argv.index("-r") + 1] == SID
    assert "--add-dir" not in argv and "--fork-session" not in argv
    assert "--always-approve" in argv
    assert "--permission-mode" not in argv


def run_turn(runner, tmp_path, timeout: int = 5):
    return asyncio.run(pg.grok_turn(SID, str(tmp_path), "hi", timeout=timeout, binary="grok", runner=runner))


def test_turn_returns_reply_and_pins_cwd(tmp_path: Path) -> None:
    seen = {}

    def runner(argv, **kw) -> "CompletedProcess[str]":
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "pong", "sessionId": SID}), "")

    assert run_turn(runner, tmp_path) == "pong"
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"]["UNIMATRIX_GROK_SESSION_ID"] == SID
    assert "--cwd" in seen["argv"] and "--add-dir" not in seen["argv"]


def test_turn_timeout_nonzero_and_garbage_return_no_reply(tmp_path: Path) -> None:
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert run_turn(timeout, tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: subprocess.CompletedProcess(a, 1, "", "err"), tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: subprocess.CompletedProcess(a, 0, "garbage", ""), tmp_path) == pg.NO_REPLY
    assert run_turn(lambda a, **k: (_ for _ in ()).throw(FileNotFoundError()), tmp_path) == pg.NO_REPLY


KEYSTROKE_PRIMITIVES = ("osascript", "send-keys", "send_keys", "tmux", "keystroke", "system events", "tiocsti",
                        "import pty", "pty.", "pexpect", "xdotool", "sendkeys", "writeconsoleinput")


def test_no_peer_adapter_or_router_types_into_a_tui() -> None:
    for path in sorted(BIN.glob("unimatrix-peer-*.py")):
        src = path.read_text(encoding="utf-8").lower()
        hits = [p for p in KEYSTROKE_PRIMITIVES if p in src]
        assert not hits, f"{path.name} carries keystroke injection: {hits}"


def test_undeliverable_turn_spawns_only_the_headless_resume_and_never_a_keystroke(tmp_path: Path) -> None:
    spawned = []

    def runner(argv, **kw) -> "CompletedProcess[str]":
        spawned.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "busy")

    assert run_turn(runner, tmp_path) == pg.NO_REPLY
    assert len(spawned) == 1 and spawned[0][:2] == ["grok", "-p"]
    assert not any(tok in " ".join(spawned[0]).lower() for tok in ("osascript", "send-keys", "tmux"))


def test_router_refuses_a_grok_peer_without_cwd_instead_of_typing(tmp_path: Path) -> None:
    router_spec = importlib.util.spec_from_file_location("unimatrix_peer_router_x", BIN / "unimatrix-peer-router.py")
    router = importlib.util.module_from_spec(router_spec)
    router_spec.loader.exec_module(router)
    reply = asyncio.run(router.default_adapters()["grok"]({"target": SID, "vendor": "grok"}, "hi"))
    assert reply.startswith("delivery failed:")


def test_own_session_matches_an_ancestor_pid_to_the_registry_pid(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    parents = {900: 500, 500: 100, 100: 1}
    peer = pg.own_session(home, pid_alive=lambda p: True, parent_of=parents.get, start=900)
    assert peer["target"] == SID and peer["name"] == pg.peer_name(SID, str(tmp_path))


def test_own_session_none_when_no_ancestor_is_a_registry_pid(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    parents = {900: 500, 500: 1}
    assert pg.own_session(home, pid_alive=lambda p: True, parent_of=parents.get, start=900) is None


def test_own_session_ignores_a_dead_registry_row_and_survives_a_parent_cycle(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 100, "cwd": str(tmp_path)}])
    assert pg.own_session(home, pid_alive=lambda p: False, parent_of={900: 100}.get, start=900) is None
    live = registry(tmp_path, [{"session_id": SID, "pid": 7, "cwd": str(tmp_path)}])
    assert pg.own_session(live, pid_alive=lambda p: True, parent_of={900: 500, 500: 900}.get, start=900) is None


def test_own_session_empty_registry_never_walks_processes(tmp_path: Path) -> None:
    def boom(pid):
        raise AssertionError("walked")

    assert pg.own_session(registry(tmp_path, []), parent_of=boom, start=1) is None
    assert pg.own_session(tmp_path / "missing", parent_of=boom, start=1) is None


def test_real_parent_pid_finds_this_processes_parent() -> None:
    assert pg.parent_pid(os.getpid()) == os.getppid()


def _chunk(text):
    return json.dumps({"method": "session/update", "params": {"update": {
        "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}}}})


def test_held_live_grok_submits_text_and_does_not_resume_headless(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": str(tmp_path), "tty": "/dev/ttys004"}])
    seen = []

    def submit(tty, text):
        seen.append((tty, text))

    def runner(argv, **kw):
        raise AssertionError("resume")

    result = asyncio.run(pg.deliver(SID, str(tmp_path), "hello from peer", home=home, binary="grok",
                                    runner=runner, is_live_grok=lambda pid: pid == 4242, submit=submit))
    assert seen == [("/dev/ttys004", "hello from peer")]
    assert result["ok"] is True and result["written"] is True and result["msg_id"]


def test_marked_session_resumes_even_when_its_grok_is_live(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": cwd, "tty": "/dev/ttys004"}])
    pg.mark_resumable(home, SID, cwd)
    path = pg.session_updates_path(home, cwd, SID)
    path.parent.mkdir(parents=True)
    path.write_text(_chunk("hello from peer") + "\n", encoding="utf-8")

    def submit(tty, text):
        raise AssertionError("tty submit")

    seen = {}

    def runner(argv, **kw) -> "CompletedProcess[str]":
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "pong"}), "")

    result = asyncio.run(pg.deliver(SID, cwd, "hello from peer", home=home, binary="grok",
                                    runner=runner, is_live_grok=lambda pid: pid == 4242, submit=submit))
    assert result["user_message_chunk"] is True and result["session_id"] == SID
    argv = seen["argv"]
    assert argv[argv.index("-r") + 1] == SID
    assert "-p" in argv and "--always-approve" in argv


def test_resumable_session_receipt_is_the_user_message_chunk(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": cwd}])
    pg.mark_resumable(home, SID, cwd)
    path = pg.session_updates_path(home, cwd, SID)
    path.parent.mkdir(parents=True)
    path.write_text(_chunk("hello from peer") + "\n", encoding="utf-8")
    seen = {}

    def submit(tty, text):
        raise AssertionError("tty submit")

    def runner(argv, **kw) -> "CompletedProcess[str]":
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "pong"}), "")

    result = asyncio.run(pg.deliver(SID, cwd, "hello from peer", home=home, binary="grok",
                                    runner=runner, submit=submit))
    assert result["user_message_chunk"] is True and result["session_id"] == SID and result["msg_id"]
    argv = seen["argv"]
    assert argv[argv.index("-r") + 1] == SID
    assert "--always-approve" in argv
    assert "--permission-mode" not in argv
    assert "--add-dir" not in argv and "--fork-session" not in argv


def test_resume_without_a_chunk_is_not_receipt(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    home = registry(tmp_path, [{"session_id": SID, "pid": 4242, "cwd": cwd}])
    pg.mark_resumable(home, SID, cwd)

    def runner(argv, **kw) -> "CompletedProcess[str]":
        return subprocess.CompletedProcess(argv, 0, json.dumps({"text": "pong"}), "")

    assert asyncio.run(pg.deliver(SID, cwd, "hi", home=home, binary="grok", runner=runner)) == (
        "delivery failed: no user_message_chunk")


def test_record_launch_marks_the_session_the_stdout_names(tmp_path: Path) -> None:
    argv = pg.launch_argv("grok", str(tmp_path), "open")
    assert "--dangerously-skip-permissions" in argv and "-r" not in argv and "--fork-session" not in argv
    assert pg.record_launch(tmp_path, json.dumps({"sessionId": SID}), str(tmp_path)) == SID
    assert pg.is_resumable(tmp_path, SID)
    assert pg.record_launch(tmp_path, "{}", str(tmp_path)) is None

