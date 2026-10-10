"""setup.py --check verifies each install fact against live state and fails on any miss."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import setup_check
from coordinator_core.install.setup_check import CheckItem
from coordinator_core.win_portability import no_console_creationflags

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_exit_code_nonzero_on_any_fail():
    assert setup_check.exit_code([CheckItem("a", True, ""), CheckItem("b", False, "")]) == 1
    assert setup_check.exit_code([CheckItem("a", True, "")]) == 0


def test_command_head_skips_env_prefix():
    assert setup_check.command_head('COORDINATOR_DOOR_STDIN_MODE=hook "/x/bin/hook-run" a b') == "/x/bin/hook-run"
    assert setup_check.command_head("python3 -m foo") == "python3"


def test_repo_pointers_report_each_key(tmp_path):
    reg = {"repos.a": str(tmp_path), "repos.b": str(tmp_path / "gone")}
    items = setup_check.check_repo_pointers(["repos.a", "repos.b", "repos.c"], reg.get)
    assert [i.ok for i in items] == [True, False, False]


def _write_settings(claude_dir: Path, payload: dict) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_statusline_pass_and_fail(tmp_path):
    assert not setup_check.check_statusline(tmp_path).ok
    _write_settings(tmp_path, {})
    assert not setup_check.check_statusline(tmp_path).ok
    script = tmp_path / "sl.py"
    script.write_text("")
    _write_settings(tmp_path, {"statusLine": {"type": "command", "command": f'FOO=1 "{script}"'}})
    assert setup_check.check_statusline(tmp_path).ok
    _write_settings(tmp_path, {"statusLine": {"type": "command", "command": str(tmp_path / "missing")}})
    assert not setup_check.check_statusline(tmp_path).ok


def test_guards_flag_unresolvable_hook_command(tmp_path):
    root = tmp_path / "root"
    (root / "coordinator_core" / "bash_guards").mkdir(parents=True)
    (root / "coordinator_core" / "bash_guards" / "dispatch.py").write_text("")
    claude = tmp_path / "claude"
    hook = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": str(tmp_path / "nope")}]}]}}
    _write_settings(claude, hook)
    assert not setup_check.check_guards(root, claude).ok
    _write_settings(claude, {})
    assert setup_check.check_guards(root, claude).ok
    assert not setup_check.check_guards(tmp_path / "empty", claude).ok


def test_heavy_admission_flags_an_unseeded_key():
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_SESSION_HEAVY_CAP
    from coordinator_core.bash_guards._heavy_admission_seed import derive_defaults

    seeded = {k: str(v) for k, v in derive_defaults(setup_check._KEY_SET_PROBE_MB).items()}
    assert setup_check.check_heavy_admission(seeded.get).ok
    partial = dict(seeded, **{KEY_SESSION_HEAVY_CAP: "0"})
    item = setup_check.check_heavy_admission(partial.get)
    assert not item.ok and KEY_SESSION_HEAVY_CAP in item.detail


def test_door_fails_when_not_installed(tmp_path):
    assert not setup_check.check_door(tmp_path).ok


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_setup_check_cli_fails_on_a_bare_settings_home(tmp_path):
    env = {
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_HOME": str(tmp_path / "home"),
        "MACHINE_LOCAL_REGISTRY_DIR": str(tmp_path / "reg"),
    }
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "setup.py"), "--check", "--claude-klabauter-live-root", str(REPO_ROOT)],
        capture_output=True, text=True, env=env, timeout=120, check=False, **no_console_creationflags(),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL [door]" in proc.stdout
    assert "FAIL [statusline]" in proc.stdout
