"""Mutating machine-local verbs hold a cross-process .registry-write.lock; readers do not."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[3]
_IMPL = _REPO / "coordinator" / "templates" / "bin" / "_machine_local.py"
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env(sandbox: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REPO_")}
    env["MACHINE_LOCAL_REGISTRY_DIR"] = str(sandbox)
    return env


def _run(sandbox: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_IMPL), *args],
        env=_env(sandbox), capture_output=True, text=True, timeout=60,
        creationflags=_NO_CONSOLE,
    )


@pytest.mark.spawns_process
def test_parallel_sets_of_distinct_keys_all_survive(tmp_path):
    (tmp_path / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    keys = [f"repos.par{i}" for i in range(6)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda k: _run(tmp_path, "set", k, f"/x/{k}"), keys))

    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    content = (tmp_path / "registry.local.toml").read_text(encoding="utf-8")
    assert [k for k in keys if f'"{k}"' not in content] == []
    assert not (tmp_path / ".registry-write.lock").exists()


@pytest.mark.spawns_process
def test_read_only_verb_ignores_a_held_lock(tmp_path):
    (tmp_path / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    (tmp_path / ".registry-write.lock").write_text("123", encoding="utf-8")
    result = _run(tmp_path, "has", "repos.nope")
    assert result.returncode == 1, result.stderr
    assert (tmp_path / ".registry-write.lock").exists()
