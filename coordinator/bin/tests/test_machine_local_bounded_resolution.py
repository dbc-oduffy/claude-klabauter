"""`_machine_local.py` read verbs are bounded: declared keys never spawn or scan,
git stalls cannot outlast the deadline, and `dump` memory is O(keys)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"
_spec = importlib.util.spec_from_file_location("_machine_local_bounded_under_test", _IMPL)
_ml = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _ml
_spec.loader.exec_module(_ml)


def _toml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


@pytest.fixture
def reg(tmp_path, monkeypatch):
    d = tmp_path / "reg"
    d.mkdir()
    for k in [k for k in os.environ if k.startswith("REPO_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    monkeypatch.setattr(_ml, "_deadline_at", None)
    return d


def _boom(*a, **k):
    raise AssertionError("subprocess spawned")


def _search_root_with_candidates(tmp_path: Path, reg: Path, slug: str, n: int) -> None:
    root = tmp_path / "roots"
    for i in range(n):
        c = root / f"copy{i}"
        (c / ".claude-plugin").mkdir(parents=True)
        (c / ".claude-plugin" / "marketplace.json").write_text(f'{{"name": "{slug}"}}')
    (reg / "search-roots.toml").write_text(f'{sys.platform} = ["{_toml_path(root)}"]\n')


def test_declared_registry_key_resolves_with_zero_spawns(reg, tmp_path, monkeypatch):
    target = tmp_path / "declared"
    target.mkdir()
    (reg / "registry.local.toml").write_text(f'"repos.foo" = "{_toml_path(target)}"\n')
    _search_root_with_candidates(tmp_path, reg, "foo", 3)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(os, "listdir", _boom)
    rc, val = _ml.resolve_one("repos.foo")
    assert rc == 0 and Path(val) == target


def test_declared_path_exception_resolves_with_zero_spawns(reg, tmp_path, monkeypatch):
    target = tmp_path / "exc"
    target.mkdir()
    (reg / "path-exceptions.toml").write_text(f'[{sys.platform}]\nfoo = "{_toml_path(target)}"\n')
    _search_root_with_candidates(tmp_path, reg, "foo", 2)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "listdir", _boom)
    rc, val = _ml.resolve_one("repos.foo")
    assert rc == 0 and Path(val) == target


def test_undeclared_key_still_autodiscovers(reg, tmp_path, monkeypatch):
    _search_root_with_candidates(tmp_path, reg, "bar", 1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    rc, val = _ml.resolve_one("repos.bar")
    assert rc == 0 and val.endswith("copy0")


def test_stalled_git_cannot_push_get_past_deadline(reg, tmp_path, monkeypatch):
    _search_root_with_candidates(tmp_path, reg, "baz", 4)
    calls = []

    def stalled_git(cmd, **kw):
        calls.append(kw["timeout"])
        time.sleep(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", stalled_git)
    monkeypatch.setattr(_ml, "_deadline_at", time.monotonic() + 0.4)
    t0 = time.monotonic()
    rc, msg = _ml.resolve_one("repos.baz")
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert rc == _ml.EXIT_OPERATIONAL and "deadline exceeded" in msg and "machine-local set repos." in msg
    assert max(calls) <= 0.4


def test_watchdog_exits_with_named_error_when_a_read_blocks(tmp_path):
    reg = tmp_path / "reg"
    reg.mkdir()
    code = (
        "import importlib.util,sys,time;"
        f"s=importlib.util.spec_from_file_location('m',r'{_IMPL}');m=importlib.util.module_from_spec(s);"
        "s.loader.exec_module(m);m._arm_deadline(0.3);time.sleep(30)"
    )
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
    assert time.monotonic() - t0 < 5
    assert p.returncode == 2 and "deadline of 0.3s exceeded" in p.stderr


def test_oversize_registry_file_is_refused(reg, monkeypatch):
    monkeypatch.setattr(_ml, "_MAX_TOML_BYTES", 100)
    (reg / "registry.local.toml").write_text("# " + "x" * 500 + "\n")
    with pytest.raises(SystemExit) as ei:
        _ml._load_toml(str(reg / "registry.local.toml"))
    assert ei.value.code == _ml.EXIT_OPERATIONAL


def test_dump_peak_memory_is_bounded_for_many_keys(reg, tmp_path, monkeypatch, capsys):
    lines = []
    for i in range(1500):
        t = tmp_path / f"r{i}"
        t.mkdir()
        lines.append(f'"repos.k{i}" = "{_toml_path(t)}"')
        lines.append(f'"paths.p{i}" = "v{i}"')
    (reg / "registry.local.toml").write_text("\n".join(lines) + "\n")
    fake_ml = tmp_path / "machine-local-big"
    for i in range(300):
        (fake_ml / "tools" / f"d{i}").mkdir(parents=True)
        (fake_ml / "tools" / f"d{i}" / "blob.bin").write_bytes(b"\0" * 20000)
    monkeypatch.setattr(sys, "argv", ["machine-local", "dump"])
    tracemalloc.start()
    try:
        assert _ml.main() == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    out = capsys.readouterr().out
    assert out.count('"repos.k') == 1500
    assert peak < 40 * 1024 * 1024, peak


def test_declared_key_with_slow_target_stat_still_resolves(reg, tmp_path, monkeypatch):
    target = tmp_path / "slow"
    target.mkdir()
    (reg / "path-exceptions.toml").write_text(f'[{sys.platform}]\nfoo = "{_toml_path(target)}"\n')
    monkeypatch.setattr(_ml, "_scan_budget_s", 0.8)
    real_exists = Path.exists

    def slow_exists(self, *a, **k):
        time.sleep(1.0)
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", slow_exists)
    rc, val = _ml.resolve_one("repos.foo")
    assert rc == 0 and Path(val) == target
