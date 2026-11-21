"""Drives coordinator/bin/codex-exec-windows.py against a fake codex on PATH."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-exec-windows.py"

_FAKE_PY = """\
import json, os, sys
data = sys.stdin.buffer.read()
open(os.environ["FAKE_CODEX_STDIN"], "wb").write(data)
home = os.environ.get("CODEX_HOME", "")
sys.stdout.write(json.dumps({
    "argv": sys.argv[1:],
    "marker": os.environ.get("FAKE_MARKER", ""),
    "codex_home": home,
    "agents_md": os.path.isfile(os.path.join(home, "AGENTS.md")) if home else False,
    "auth": os.path.isfile(os.path.join(home, "auth.json")) if home else False,
}) + "\\n")
sys.stdout.flush()
sys.stdout.buffer.write("caf\\u00e9\\n".encode("utf-8"))
sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
"""


def _load():
    spec = importlib.util.spec_from_file_location("codex_exec_windows", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import_is_inert():
    assert _load().main is not None


def test_writable_root_prefers_cd_arg_then_cwd(tmp_path, monkeypatch):
    mod = _load()
    assert mod.writable_root_of(["--json", "-C", str(tmp_path)]) == tmp_path
    assert mod.writable_root_of([f"--cd={tmp_path}"]) == tmp_path
    monkeypatch.chdir(tmp_path)
    assert mod.writable_root_of(["--json"]) == Path.cwd()


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
