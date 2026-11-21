"""percolate-round removes its pid-stamped scratch dir on every exit and reaps dead-pid dirs."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

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


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_dead_pid_dir_reaped_live_pid_dir_kept_at_round_start(round_env) -> None:
    dead = round_env.root / f"percolate-round-{_dead_pid()}-abcd1234"
    live = round_env.root / f"percolate-round-{os.getpid()}-live0000"
    for d in (dead, live):
        d.mkdir()
        (d / "f.txt").write_text("x")

    def _during() -> int:
        assert not dead.exists()
        assert live.exists()
        return 0

    round_env.seen["action"] = _during
    assert _mod._cmd_round(round_env.args) == 0
    assert not dead.exists()
    assert live.exists()
