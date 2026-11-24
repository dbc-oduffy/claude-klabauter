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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.complete

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_module() -> ModuleType:
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
def round_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
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

    def _default(args, target, root, src, dest, tmp, **_kwargs):
        seen["tmp"] = tmp
        assert tmp.is_dir()
        assert tmp.name.startswith(f"percolate-round-{os.getpid()}-")
        return seen["action"]()

    monkeypatch.setattr(_mod, "_cmd_round_default", _default)
    args = SimpleNamespace(
        target="alpha", percolate_root=None, dest_root=None, source_ref=None,
        allow_source_rollback=False, no_publish=False,
    )
    return SimpleNamespace(args=args, seen=seen, root=tmp_path)


def test_success_removes_dir(round_env) -> None:
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == 0
    assert not round_env.seen["tmp"].exists()
    assert _round_dirs(round_env.root) == []


def test_refusal_leaves_no_dir(round_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_mod, "_registry_integrity_refusal", lambda *a, **k: "refused")
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == _mod._EXIT_FAIL
    assert _round_dirs(round_env.root) == []


def test_nonzero_return_removes_dir(round_env) -> None:
    round_env.seen["action"] = lambda: _mod._EXIT_FAIL
    assert _mod._cmd_round(round_env.args) == _mod._EXIT_FAIL
    assert _round_dirs(round_env.root) == []


def test_raised_failure_removes_dir(round_env) -> None:
    def _boom():
        raise RuntimeError("boom")

    round_env.seen["action"] = _boom
    with pytest.raises(RuntimeError):
        _mod._cmd_round(round_env.args)
    assert _round_dirs(round_env.root) == []


def test_publish_timeout_keeps_dir_and_names_progress(round_env, capsys) -> None:
    def _timeout():
        (round_env.seen["tmp"] / "publish-progress.json").write_text("{}", encoding="utf-8")
        raise subprocess.TimeoutExpired(["publish.py"], 1)

    round_env.seen["action"] = _timeout
    with pytest.raises(subprocess.TimeoutExpired):
        _mod._cmd_round(round_env.args)
    kept = round_env.seen["tmp"]
    assert (kept / "publish-progress.json").is_file()
    err = capsys.readouterr().err
    assert str(kept) in err and "publish-progress.json" in err


def test_kept_timeout_dir_reaped_once_pid_dead(round_env, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = round_env.root / "percolate-round-999999-abc"
    dead.mkdir()
    monkeypatch.setattr(_mod, "_pid_is_alive", lambda pid: False)
    round_env.seen["action"] = lambda: 0
    assert _mod._cmd_round(round_env.args) == 0
    assert not dead.exists()


def test_reaper_setup_failure_logs_typed_advisory_and_never_raises(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _broken():
        raise ImportError("no temp_layout")

    monkeypatch.setattr(_mod, "_temp_layout", _broken)
    _mod._reap_dead_round_tempdirs()
    err = capsys.readouterr().err
    assert "advisory" in err and "ImportError" in err and "no temp_layout" in err


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_signal_unwinds_and_removes_dir(round_env, signame) -> None:
    prior = signal.getsignal(getattr(signal, signame))

    def _kill_self() -> int:
        os.kill(os.getpid(), getattr(signal, signame))
        time.sleep(5)
        return 0

    round_env.seen["action"] = _kill_self
    with pytest.raises(SystemExit):
        _mod._cmd_round(round_env.args)
    assert _round_dirs(round_env.root) == []
    assert signal.getsignal(getattr(signal, signame)) == prior


def test_legacy_unstamped_dir_reaped_only_when_over_a_day_old(round_env) -> None:
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
