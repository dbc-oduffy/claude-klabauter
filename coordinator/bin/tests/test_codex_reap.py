"""reap_codex_home kills codex daemons owned by a root and nothing else, with no child spawn."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1]
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _unimatrix import codex_reap  # noqa: E402
from _unimatrix.proc import pid_alive  # noqa: E402

pytest.importorskip("psutil")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SLEEPER = "import time\ntime.sleep(120)\n"


def _stub(directory: Path, name: str = "codex.py") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(SLEEPER, encoding="utf-8")
    return path


def _spawn(argv, env=None):
    proc = subprocess.Popen(
        [sys.executable, *argv], env=env, creationflags=NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pid_alive(proc.pid):
        time.sleep(0.02)
    time.sleep(0.2)
    return proc


@pytest.fixture
def procs():
    started = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)


def test_reaps_argv_path_under_root_and_spares_decoy(tmp_path, procs):
    root = tmp_path / "scratch"
    inside = _spawn([str(_stub(root / "home" / ".codex")), "app-server", "--managed-daemon"])
    decoy = _spawn([str(_stub(tmp_path / "elsewhere" / ".codex")), "app-server", "--managed-daemon"])
    procs += [inside, decoy]

    killed = codex_reap.reap_codex_home(root)

    assert killed == [inside.pid]
    assert not pid_alive(inside.pid)
    assert decoy.poll() is None


def test_reaps_pid_update_loop_marker(tmp_path, procs):
    root = tmp_path / "scratch"
    loop = _spawn([str(_stub(root / "home" / ".codex")), "pid-update-loop"])
    procs.append(loop)

    assert codex_reap.reap_codex_home(root) == [loop.pid]


def test_reaps_by_codex_home_env_when_argv_is_outside(tmp_path, procs):
    root = tmp_path / "scratch"
    env = {**os.environ, "CODEX_HOME": str(root / "home" / ".codex")}
    owned = _spawn([str(_stub(tmp_path / "bin")), "app-server"], env=env)
    other = _spawn([str(_stub(tmp_path / "bin2")), "app-server"],
                   env={**os.environ, "CODEX_HOME": str(tmp_path / "unrelated")})
    procs += [owned, other]

    assert codex_reap.reap_codex_home(root) == [owned.pid]
    assert other.poll() is None


def test_spares_codex_without_marker_and_sibling_prefix_root(tmp_path, procs):
    root = tmp_path / "scratch"
    tui = _spawn([str(_stub(root / "home")), "--interactive"])
    sibling = _spawn([str(_stub(tmp_path / "scratch2")), "app-server"])
    procs += [tui, sibling]

    assert codex_reap.reap_codex_home(root) == []
    assert tui.poll() is None and sibling.poll() is None


def test_spares_marker_argv_that_does_not_name_codex(tmp_path, procs):
    root = tmp_path / "scratch"
    other = _spawn([str(_stub(root, "worker.py")), "app-server"])
    procs.append(other)

    assert codex_reap.reap_codex_home(root) == []
    assert other.poll() is None


def test_reap_spawns_no_child_process(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("reap_codex_home spawned a child process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)

    assert codex_reap.reap_codex_home(tmp_path / "absent") == []
