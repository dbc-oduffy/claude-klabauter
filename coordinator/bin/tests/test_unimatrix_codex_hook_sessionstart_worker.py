"""Behavior pins for the Codex SessionStart hook's worker doctrine pointer."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_ss", BIN / "unimatrix-codex-hook-sessionstart.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

wm_spec = importlib.util.spec_from_file_location(
    "wm", BIN.parents[1] / "coordinator_core" / "agent_pipe" / "worker_mode.py")
wm = importlib.util.module_from_spec(wm_spec)
wm_spec.loader.exec_module(wm)

PAYLOAD = {"session_id": "thread-1", "cwd": "/nowhere"}


def test_env_name_comes_from_worker_mode():
    assert hook.worker_env_name() == wm.WORKER_ENV


def test_marker_set_injects_pointer_alongside_registration(tmp_path, monkeypatch):
    monkeypatch.setenv(wm.WORKER_ENV, "1")
    out = hook.run(PAYLOAD, tmp_path / "peers.json")
    assert out["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": hook.WORKER_POINTER}
    assert "systemMessage" in out
    json.dumps(out)


def test_marker_set_injects_pointer_even_without_a_thread_id(tmp_path, monkeypatch):
    monkeypatch.setenv(wm.WORKER_ENV, "1")
    out = hook.run({}, tmp_path / "peers.json")
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": hook.WORKER_POINTER}}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_marker_unset_or_blank_injects_nothing(tmp_path, monkeypatch, value):
    monkeypatch.delenv(wm.WORKER_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(wm.WORKER_ENV, value)
    out = hook.run(PAYLOAD, tmp_path / "peers.json")
    assert "hookSpecificOutput" not in out
    assert hook.run({}, tmp_path / "peers.json") is None


def test_thread_without_engine_address_registers_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(wm.WORKER_ENV, raising=False)
    peers = tmp_path / "peers.json"
    out = hook.run({"session_id": "thread-1", "cwd": str(tmp_path / "repo")}, peers, tmp_path / "bindings")
    assert "unbound" in out["systemMessage"] and "thread-1" in out["systemMessage"]
    assert not peers.exists()


THREAD = "01a1b629-5c61-7181-9f97-51efc1cc9a6c"
ADDRESS = {"schema_version": 1, "provider": "codex", "session_id": THREAD, "address_version": 1,
           "name": "um01-codex-proj-1", "box": "um01"}


def first_row(peers):
    return json.loads(peers.read_text())[0]


def test_register_peer_writes_holder_and_rebind_refreshes_it(tmp_path):
    peers = tmp_path / "peers.json"
    first = {"codexPid": 111, "codexProcStart": "aaa"}
    assert hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS, holder=first) == "added"
    row = first_row(peers)
    assert (row["codexPid"], row["codexProcStart"]) == (111, "aaa")
    assert hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS, holder=first) == "present"
    assert hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS,
                              holder={"codexPid": 222, "codexProcStart": "bbb"}) == "rebound"
    row = first_row(peers)
    assert (row["codexPid"], row["codexProcStart"]) == (222, "bbb")
    hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS, holder={"codexPid": 333})
    row = first_row(peers)
    assert row["codexPid"] == 333 and "codexProcStart" not in row


def test_register_without_holder_keeps_prior_holder(tmp_path):
    peers = tmp_path / "peers.json"
    hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS, holder={"codexPid": 111, "codexProcStart": "aaa"})
    assert hook.register_peer(peers, "n", "codex", THREAD, address=ADDRESS) == "present"
    assert first_row(peers)["codexPid"] == 111


def test_codex_holder_defaults_to_parent_pid_with_its_proc_start(monkeypatch):
    monkeypatch.setattr(hook.os, "getppid", lambda: 4242)
    liveness = hook.load_sibling("unimatrix-liveness")
    monkeypatch.setattr(hook, "load_sibling", lambda name: liveness)
    monkeypatch.setattr(liveness, "proc_start", lambda pid: f"start-{pid}")
    assert hook.codex_holder() == {"codexPid": 4242, "codexProcStart": "start-4242"}


def test_codex_holder_unreadable_start_keeps_pid_and_bad_pid_gives_nothing(monkeypatch):
    liveness = hook.load_sibling("unimatrix-liveness")
    monkeypatch.setattr(hook, "load_sibling", lambda name: liveness)

    def boom(pid):
        raise OSError("gone")

    monkeypatch.setattr(liveness, "proc_start", boom)
    assert hook.codex_holder(77) == {"codexPid": 77}
    assert hook.codex_holder(0) == {}


def test_run_records_parent_as_holder_on_the_peer_row(tmp_path, monkeypatch):
    monkeypatch.delenv(wm.WORKER_ENV, raising=False)
    monkeypatch.setattr(hook.socket, "gethostname", lambda: "um01")
    monkeypatch.setattr(hook, "codex_holder", lambda: {"codexPid": 9, "codexProcStart": "s"})
    registry = tmp_path / "bindings"
    registry.mkdir()
    (registry / f"{THREAD}.json").write_text(json.dumps(ADDRESS))
    peers = tmp_path / "peers.json"
    out = hook.run({"session_id": THREAD, "cwd": str(tmp_path)}, peers, registry)
    assert "bound to Codex thread" in out["systemMessage"]
    row = first_row(peers)
    assert (row["codexPid"], row["codexProcStart"]) == (9, "s")


def test_pointer_names_doctrine_path_only():
    assert "worker-mode.md" in hook.WORKER_POINTER
