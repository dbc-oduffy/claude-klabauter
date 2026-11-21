"""A child spawned with the workflow or grok env resolves a machine-local key and keeps the user-profile context."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.codex_runner import grok, workflow

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROFILE_KEYS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH")


@pytest.fixture
def settings_home(tmp_path, monkeypatch):
    template = Path(__file__).resolve().parents[3] / "coordinator" / "templates" / "bin"
    home = tmp_path / "settings"
    (home / "bin").mkdir(parents=True)
    for name in ("_machine_local.py", "claude_machine_local.py"):
        shutil.copy(template / name, home / "bin" / name)
    (home / "machine-local").mkdir()
    (home / "machine-local" / "registry.local.toml").write_text(
        "[repos]\nprobe_repo = '" + str(tmp_path) + "'\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    for key in _PROFILE_KEYS:
        monkeypatch.setenv(key, str(tmp_path / key))
    return home


def _grok_env():
    return grok._build_env()


@pytest.mark.parametrize("build_env", [workflow._environment, _grok_env], ids=["workflow", "grok"])
def test_spawned_child_resolves_machine_local_key(build_env, settings_home, tmp_path):
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from claude_machine_local import repos; print(repos.probe_repo)", str(settings_home / "bin")],
        env=build_env(), capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()) == tmp_path
