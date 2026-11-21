"""Behavior pins for the spawn-free unimatrix liveness module."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
SRC = BIN / "unimatrix-liveness.py"


def _load() -> "ModuleType":
    spec = importlib.util.spec_from_file_location("unimatrix_liveness", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lv = _load()


@pytest.mark.skipif(sys.platform == "win32", reason="ps lstart is POSIX")
def test_proc_start_matches_ps_lstart_for_child_pid() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        want = subprocess.run(["ps", "-o", "lstart=", "-p", str(child.pid)],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert lv.proc_start(child.pid) == want
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="posix pid")
def test_proc_start_dead_pid_raises_oserror() -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    with pytest.raises(OSError):
        lv.proc_start(p.pid)


def _rec(pid=None):
    pid = pid or os.getpid()
    return {"pid": pid, "procStart": lv.proc_start(pid)}


@pytest.fixture(autouse=True)
def _agy_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point agy's brain root under tmp so presence locks never read the real home."""
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "agy" / "brain"))
    presence = tmp_path / "agy" / "presence"
    presence.mkdir(parents=True)
    return presence


def test_codex_probe_cli_prints_one_line_per_codex_peer(tmp_path: Path) -> None:
    me = _rec()
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / "thr-live.lock").write_text("")
    (locks / "thr-orphan.lock").write_text("")
    (locks / ".hidden.lock").write_text("")
    peers = tmp_path / "peers.json"
    peers.write_text(json.dumps({"peers": [
        {"name": "codex-a", "vendor": "codex", "target": "thr-live", "cwd": "/w/a",
         "codexPid": me["pid"], "codexProcStart": me["procStart"]},
        {"name": "codex-b", "vendor": "codex", "target": "thr-dead", "cwd": "/w/b",
         "codexPid": me["pid"], "codexProcStart": "Mon Jan  1 00:00:00 2001"},
        {"name": "codex-c", "vendor": "codex", "target": "thr-none"},
        {"name": "claude-x", "vendor": "claude", "target": "t"},
    ]}))
    out = subprocess.run([sys.executable, str(SRC), "codex-probe", "--peers", str(peers),
                          "--locks-dir", str(locks)], capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [
        f"codex-a thr-live {me['pid']} /w/a live",
        f"codex-b thr-dead {me['pid']} /w/b stale",
        "codex-c thr-none - - stale",
        "codex-thr-orphan thr-orphan - - stale",
    ]


def test_codex_probe_cli_rejects_unknown_command() -> None:
    r = subprocess.run([sys.executable, str(SRC), "bogus"], capture_output=True, text=True)
    assert r.returncode == 2
