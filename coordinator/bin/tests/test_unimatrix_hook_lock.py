"""Concurrent SessionStart hooks must not overwrite each other's registrations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


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
