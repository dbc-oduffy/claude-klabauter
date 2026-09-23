"""test_with_tier_t_slot_win32_shim_resolution.py -- coverage for the win32
argv[0] resolution `with-tier-t-slot` added to fix
state/cross-repo/inbox/2026-09-23-example-cockpit-repo-em-with-tier-t-slot-cannot-launch-npm-bin-shims-on-win32.md.

`subprocess.Popen([shim_path, *args])` on win32 cannot launch an npm
`node_modules/.bin/<name>` shim in ANY of its three shapes (extensionless
POSIX shim: WinError 193; `.cmd`: cmd.exe parse error; a bare relative
`.cmd` path: WinError 2) -- only `node <script>` worked. The fix parses an
npm `cmd-shim`-format `.cmd` for its JS target and launches
`[node, <target>, *args]` directly -- a plain argv, never `cmd.exe`, never
`shell=True` (EM ruling 2026-09-23: no shell-out for this fix).

This suite pins:
  1. A cmd-shim-format `.cmd` (extensionless argv[0], PATHEXT-resolved) is
     parsed to its JS target and launched with the original args intact.
  2. A non-npm `.cmd` (arbitrary batch content) fails loudly with a
     one-line message naming the target and the `node <script>` workaround
     -- never silently, never via cmd.exe.
  3. The POSIX path (`sys.platform != "win32"`) is a no-op passthrough.

Loads `with-tier-t-slot` in-process (no `.py` suffix, not import-
discoverable), mirroring `test_with_suite_mutex.py::_load_wrapper_module`,
so the pure resolution functions are reachable without a subprocess
boundary.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "coordinator" / "bin" / "with-tier-t-slot"


def _load_wrapper_module():
    loader = importlib.machinery.SourceFileLoader(
        "with_tier_t_slot_under_test", str(_WRAPPER)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture()
def wrapper():
    return _load_wrapper_module()


def _write_cmd_shim(bin_dir: Path, name: str, js_rel: str) -> Path:
    """Write an npm `cmd-shim`-format `.cmd` at `bin_dir/<name>.cmd`, whose
    invocation line targets `js_rel` (relative to `bin_dir`), matching the
    real template cmd-shim emits (npm's own `lib/cmd-shim.js`)."""
    shim = bin_dir / f"{name}.cmd"
    shim.write_text(
        textwrap.dedent(
            f"""\
            @ECHO off
            SETLOCAL
            CALL :find_dp0

            IF EXIST "%dp0%\\node.exe" (
              SET "_prog=%dp0%\\node.exe"
            ) ELSE (
              SET "_prog=node"
              SET PATHEXT=%PATHEXT:;.JS;=;%
            )

            "%_prog%"  "%dp0%\\{js_rel}" %*
            """
        ),
        encoding="utf-8",
    )
    return shim


def test_win32_npm_shim_resolves_target_and_preserves_args(tmp_path, wrapper, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wrapper, "is_executable", lambda p: os.path.isfile(p))
    # abs-path-ok: fake sentinel path asserting which() output passes through untouched, not a real host path
    monkeypatch.setattr(wrapper.shutil, "which", lambda name: r"C:\fake\node.exe" if name == "node" else None)

    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    js_target = tmp_path / "node_modules" / "typescript" / "bin" / "tsc"
    js_target.parent.mkdir(parents=True)
    js_target.write_text("console.log(process.argv.slice(2).join(' '))\n", encoding="utf-8")
    _write_cmd_shim(bin_dir, "tsc", r"..\typescript\bin\tsc")

    # Extensionless argv[0] -- the exact shape the memo's first failing row used.
    argv0 = str(bin_dir / "tsc")
    result = wrapper._win32_resolve_command([argv0, "--version"])

    assert result is not None
    assert result[0] == r"C:\fake\node.exe"  # abs-path-ok: same fake sentinel, asserted round-trip
    assert result[1] == os.path.normpath(str(js_target))
    assert result[2:] == ["--version"]


def test_win32_non_npm_cmd_fails_loudly(tmp_path, wrapper, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wrapper, "is_executable", lambda p: os.path.isfile(p))

    shim = tmp_path / "some-tool.cmd"
    shim.write_text("@ECHO off\necho hello\n", encoding="utf-8")

    result = wrapper._win32_resolve_command([str(shim), "--flag"])

    assert result is None
    err = capsys.readouterr().err
    assert shim.name in err
    assert "node <script>" in err


def test_posix_path_is_a_passthrough(wrapper, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    command = ["./node_modules/.bin/tsc", "--version"]
    assert wrapper._win32_resolve_command(command) == command
