"""Drives coordinator/bin/codex-exec-windows.py against a fake codex on PATH."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-exec-windows.py"

_FAKE_PY = """\
import json, os, sys
data = sys.stdin.buffer.read()
open(os.environ["FAKE_CODEX_STDIN"], "wb").write(data)
sys.stdout.write(json.dumps({"argv": sys.argv[1:], "marker": os.environ.get("FAKE_MARKER", "")}) + "\\n")
sys.stdout.flush()
sys.stdout.buffer.write("caf\\u00e9\\n".encode("utf-8"))
sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
"""


def _install_fake_codex(directory: Path, marker: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    impl = directory / "fake_codex_impl.py"
    impl.write_text(_FAKE_PY.replace('os.environ.get("FAKE_MARKER", "")', repr(marker)), encoding="utf-8")
    if os.name == "nt":
        (directory / "codex.cmd").write_text(f'@"{sys.executable}" "{impl}" %*\r\n', encoding="utf-8")
    else:
        shim = directory / "codex"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)


def _load():
    spec = importlib.util.spec_from_file_location("codex_exec_windows", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_path: Path, path_dirs: list[Path], rc: int = 0, extra: list[str] | None = None):
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes("héllo prompt\n".encode("utf-8"))
    events = tmp_path / "events.jsonl"
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(str(d) for d in path_dirs)
    env["FAKE_CODEX_STDIN"] = str(tmp_path / "stdin.bin")
    env["FAKE_CODEX_RC"] = str(rc)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--prompt", str(prompt), "--events", str(events), *(extra or [])],
        env=env,
        capture_output=True,
        timeout=60,
    )
    return proc, events


def test_help_exits_zero():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert b"--prompt" in proc.stdout


def test_import_is_inert():
    assert _load().main is not None


def test_stdin_events_args_and_zero_exit(tmp_path):
    good = tmp_path / "good"
    _install_fake_codex(good, "good")
    proc, events = _run(tmp_path, [good], extra=["--json", "-m", "gpt"])
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "stdin.bin").read_bytes() == "héllo prompt\n".encode("utf-8")
    lines = events.read_bytes().decode("utf-8").splitlines()
    assert json.loads(lines[0])["argv"] == ["exec", "--json", "-m", "gpt"]
    assert lines[1] == "café"


@pytest.mark.parametrize("rc", [3, 42])
def test_exit_code_passthrough(tmp_path, rc):
    good = tmp_path / "good"
    _install_fake_codex(good, "good")
    proc, _ = _run(tmp_path, [good], rc=rc)
    assert proc.returncode == rc


def test_store_alias_entry_is_skipped(tmp_path):
    stub = tmp_path / "Microsoft" / "WindowsApps"
    good = tmp_path / "good"
    _install_fake_codex(stub, "stub")
    _install_fake_codex(good, "good")
    proc, events = _run(tmp_path, [stub, good])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(events.read_text(encoding="utf-8").splitlines()[0])["marker"] == "good"


def test_only_store_alias_codex_refuses(tmp_path):
    stub = tmp_path / "WindowsApps"
    _install_fake_codex(stub, "stub")
    proc, _ = _run(tmp_path, [stub])
    assert proc.returncode == 127
    assert b"codex not found" in proc.stderr


def test_clean_path_filters_and_prepends_pwsh(tmp_path):
    mod = _load()
    stub = tmp_path / "WindowsApps"
    other = tmp_path / "other"
    pwshdir = tmp_path / "pwshdir"
    for d in (stub, other, pwshdir):
        d.mkdir()
    name = "pwsh.exe" if os.name == "nt" else "pwsh"
    exe = pwshdir / name
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    joined = os.pathsep.join(str(d) for d in (stub, other, pwshdir))
    result = mod.clean_path(joined).split(os.pathsep)
    assert result == [str(pwshdir), str(other)]
