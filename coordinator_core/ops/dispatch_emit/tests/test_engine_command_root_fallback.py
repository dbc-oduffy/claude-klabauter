"""The portable engine bootstrap resolves the engine root from machine-local when the agent shell
inherits no COORDINATOR_ENGINE_ROOT, instead of dying ModuleNotFoundError (a zero-run test leg)."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from coordinator_core.ops.dispatch_emit.engine_command import engine_module_command

ENGINE = Path(__file__).resolve().parents[4]


def _argv(module: str, args: str) -> list[str]:
    argv = shlex.split(engine_module_command(module, ".", args, portable=True, shell="bash"))
    # -S: this interpreter's site-packages may already expose coordinator_core, which would hide the fallback.
    return [sys.executable, "-S", *argv[1:]]


def _fake_machine_local(bin_dir: Path, value: str) -> None:
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "machine-local.cmd").write_text(f"@echo {value}\r\n", encoding="utf-8")
    else:
        script = bin_dir / "machine-local"
        script.write_text(f"#!/bin/sh\necho '{value}'\n", encoding="utf-8")
        script.chmod(0o755)


def _env(bin_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("COORDINATOR_ENGINE_ROOT", "PYTHONPATH")}
    env["PATH"] = str(bin_dir)
    return env


def _run(tmp_path: Path, value: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    _fake_machine_local(bin_dir, value)
    cwd = tmp_path / "consumer"
    cwd.mkdir()
    return subprocess.run(_argv("coordinator_core.ops.test_leg_run", ""), cwd=cwd, env=_env(bin_dir),
                          capture_output=True, text=True, timeout=120)


def test_unset_engine_root_resolves_through_machine_local(tmp_path):
    proc = _run(tmp_path, str(ENGINE))
    # Under -S a third-party engine dep (yaml) may be missing; the engine package itself must be found.
    assert "No module named 'coordinator_core'" not in proc.stderr
    last = (proc.stderr.strip().splitlines() or [""])[-1]
    assert not last.startswith("coordinator engine root unresolved")


def test_unresolvable_engine_root_exits_with_the_named_remedy(tmp_path):
    proc = _run(tmp_path, str(tmp_path / "no-engine-here"))
    assert proc.returncode != 0
    assert "ModuleNotFoundError" not in proc.stderr
    assert "machine-local set repos.claude_klabauter" in proc.stderr


def test_the_bootstrap_never_names_the_claude_klabauter_registry_key():
    assert "repos.claude_klabauter" not in engine_module_command("m", ".", "", portable=True, shell="bash")
