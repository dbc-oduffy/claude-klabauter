"""percolate-round removes its pid-stamped scratch dir on every exit and reaps dead-pid dirs."""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.complete

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_temp_dir_cleanup", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _round_dirs(root: Path) -> list:
    return sorted(p.name for p in root.glob("percolate-round-*"))


@pytest.fixture
def round_env(tmp_path, monkeypatch):
    """Drive `_cmd_round` with every preflight green and tmp_path as the temp root."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    if str(_REPO_ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(_REPO_ROOT))
    seen = {}

    monkeypatch.setattr(_mod, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(_mod, "_unresolved_root_flag", lambda a: None)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda r: "root")
    monkeypatch.setattr(_mod, "_branch0_gate", lambda t, r: "src")
    monkeypatch.setattr(_mod, "_resolve_dest", lambda t, r: "dest")
    monkeypatch.setattr(_mod, "_resolve_shipped_sha", lambda *a, **k: "abc")
    monkeypatch.setattr(_mod, "_fold_bar_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_registry_integrity_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_field7_preflight", lambda *a, **k: None)
    fake = types.ModuleType("coordinator_core.percolate.source_preflight")
    fake.check_source_published = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "coordinator_core.percolate.source_preflight", fake)

    def _default(args, target, root, src, dest, tmp, override=None):
        seen["tmp"] = tmp
        assert tmp.is_dir()
        assert tmp.name.startswith(f"percolate-round-{os.getpid()}-")
        return seen["action"]()

    monkeypatch.setattr(_mod, "_cmd_round_default", _default)
    args = SimpleNamespace(
        target="alpha", percolate_root=None, dest_root=None, source_ref=None,
        allow_source_rollback=False,
    )
    return SimpleNamespace(args=args, seen=seen, root=tmp_path)


def test_success_removes_dir(round_env):
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == 0
    assert not round_env.seen["tmp"].exists()
    assert _round_dirs(round_env.root) == []


def test_refusal_leaves_no_dir(round_env, monkeypatch):
    monkeypatch.setattr(_mod, "_registry_integrity_refusal", lambda *a, **k: "refused")
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == _mod._EXIT_FAIL
    assert _round_dirs(round_env.root) == []


def test_nonzero_return_removes_dir(round_env):
    round_env.seen["action"] = lambda: _mod._EXIT_FAIL
    assert _mod._cmd_round(round_env.args) == _mod._EXIT_FAIL
    assert _round_dirs(round_env.root) == []


def test_raised_failure_removes_dir(round_env):
    def _boom():
        raise RuntimeError("boom")

    round_env.seen["action"] = _boom
    with pytest.raises(RuntimeError):
        _mod._cmd_round(round_env.args)
    assert _round_dirs(round_env.root) == []


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_signal_unwinds_and_removes_dir(round_env, signame):
    prior = signal.getsignal(getattr(signal, signame))

    def _kill_self():
        os.kill(os.getpid(), getattr(signal, signame))
        time.sleep(5)
        return 0

    round_env.seen["action"] = _kill_self
    with pytest.raises(SystemExit):
        _mod._cmd_round(round_env.args)
    assert _round_dirs(round_env.root) == []
    assert signal.getsignal(getattr(signal, signame)) == prior


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_dead_pid_dir_reaped_live_pid_dir_kept_at_round_start(round_env):
    dead = round_env.root / f"percolate-round-{_dead_pid()}-abcd1234"
    live = round_env.root / f"percolate-round-{os.getpid()}-live0000"
    for d in (dead, live):
        d.mkdir()
        (d / "f.txt").write_text("x")

    def _during():
        assert not dead.exists()
        assert live.exists()
        return 0

    round_env.seen["action"] = _during
    assert _mod._cmd_round(round_env.args) == 0
    assert not dead.exists()
    assert live.exists()


def test_legacy_unstamped_dir_reaped_only_when_over_a_day_old(round_env):
    old = round_env.root / "percolate-round-i98oqar_"
    fresh = round_env.root / "percolate-round-fresh123"
    for d in (old, fresh):
        d.mkdir()
    stale = time.time() - 2 * 24 * 3600
    os.utime(old, (stale, stale))
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == 0
    assert not old.exists()
    assert fresh.exists()
