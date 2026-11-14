"""Concurrent SessionStart hooks must not overwrite each other's registrations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


def _hook():
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "unimatrix-codex-hook-sessionstart.py"
    spec = importlib.util.spec_from_file_location("hook_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [
    {"name": "agy-dead", "vendor": "agy", "target": "dead-1", "extra": 1},
    {"name": "agy-live", "vendor": "agy", "target": "live-1"},
    {"name": "codex-same-target", "vendor": "codex", "target": "dead-1"},
]


def test_drop_peer_rows_removes_only_named_agy_targets_and_keeps_the_rest(tmp_path):
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    assert hook.drop_peer_rows(peers, {"dead-1", "ghost"}) == ["agy-dead"]
    assert json.loads(peers.read_text()) == ROWS[1:]


def test_drop_peer_rows_preserves_the_peers_container_shape(tmp_path):
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps({"version": 2, "peers": ROWS}))
    assert hook.drop_peer_rows(peers, {"dead-1"}) == ["agy-dead"]
    assert json.loads(peers.read_text()) == {"version": 2, "peers": ROWS[1:]}


def test_drop_peer_rows_writes_nothing_when_nothing_matches(tmp_path):
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    before = peers.stat().st_mtime_ns
    assert hook.drop_peer_rows(peers, {"ghost"}) == []
    assert peers.stat().st_mtime_ns == before
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


@pytest.mark.parametrize("content", ["{not json", '{"peers": "x"}', '"str"'])
def test_drop_peer_rows_never_writes_an_unparseable_or_non_list_file(tmp_path, content):
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(content)
    assert hook.drop_peer_rows(peers, {"dead-1"}) == []
    assert peers.read_text() == content


def test_drop_peer_rows_missing_file_is_a_noop(tmp_path):
    hook = _hook()
    assert hook.drop_peer_rows(tmp_path / "peers.json", {"dead-1"}) == []
    assert not (tmp_path / "peers.json").exists()


def test_drop_peer_rows_runs_under_the_peers_lock(tmp_path, monkeypatch):
    hook = _hook()
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps(ROWS))
    seen = []
    real = hook._peers_lock

    import contextlib

    @contextlib.contextmanager
    def spy(path):
        seen.append(("enter", Path(path)))
        with real(path):
            yield
        seen.append(("exit", Path(path)))

    monkeypatch.setattr(hook, "_peers_lock", spy)
    hook.drop_peer_rows(peers, {"dead-1"})
    assert seen == [("enter", peers), ("exit", peers)]


def test_register_peer_still_registers_through_the_shared_lock(tmp_path):
    hook = _hook()
    peers = tmp_path / "peers.json"
    assert hook.register_peer(peers, "p1", "codex", "t1") == "added"
    assert hook.register_peer(peers, "p1", "codex", "t1") == "present"
    assert [r["target"] for r in json.loads(peers.read_text())] == ["t1"]


@pytest.mark.skipif(os.name != "nt", reason="Windows registration lock")
def test_windows_hooks_serialize_the_entire_read_modify_write(tmp_path):
    script = Path(__file__).resolve().parents[1] / "unimatrix-codex-hook-sessionstart.py"
    peers = tmp_path / "peers.json"
    release = tmp_path / "release-first"
    worker = r'''
import importlib.util
from pathlib import Path
import sys
import time
script, peers, marker, release, ident = sys.argv[1:]
spec = importlib.util.spec_from_file_location("hook_worker", script)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
Path(marker + ".started").touch()
def pick(rows):
    Path(marker).touch()
    if ident == "first":
        deadline = time.monotonic() + 10
        while not Path(release).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test did not release the first hook")
            time.sleep(0.01)
    return "peer-" + ident
hook.register_peer(peers, pick, "codex", ident)
'''

    def wait_for(path):
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, f"worker did not reach {path.name}"
            time.sleep(0.01)

    processes = []
    try:
        for ident in ("first", "second"):
            marker = tmp_path / ident
            processes.append(subprocess.Popen(
                [sys.executable, "-c", worker, str(script), str(peers), str(marker), str(release), ident],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            wait_for(marker if ident == "first" else Path(str(marker) + ".started"))
        time.sleep(0.15)
        assert not (tmp_path / "second").exists(), "second hook entered while first held the lock"
        release.touch()
        for process in processes:
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error.decode(errors="replace")
        assert {row["target"] for row in json.loads(peers.read_text())} == {"first", "second"}
    finally:
        release.touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
