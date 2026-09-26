"""
coordinator_core.ops.whoami_run_tests — venv-provisioning pytest launcher for
the coordinator_whoami standalone package.

Purpose: claude-klabauter-native port of the DoE-owned `coordinator/whoami/run-tests.sh`
CLI trampoline (DR-047 contract-vs-engine split). `coordinator_whoami` is a
standalone installable package (its own pyproject.toml, editable install,
jsonschema dependency) nested inside the DoE meta-repo — bare `pytest` under
the meta-repo's ambient interpreter cannot run its tests (missing editable
install, missing deps, possible stale-installed-copy shadowing). This module
owns the pure provisioning/launch logic; the DoE trampoline owns resolving
its own directory and passing it in.

Behavior mirrors the bash oracle line-for-line:
  1. Resolve `base_dir` (the DoE trampoline passes its own directory — the
     bash oracle used `cd "$(dirname "${BASH_SOURCE[0]}")"`).
  2. `.venv/.deps-installed` sentinel absent → provision: `python3 -m venv
     .venv` (if `.venv` dir itself absent), then inside that venv:
     `pip install --quiet --upgrade pip`, `pip install --quiet -e .`,
     `pip install --quiet pytest`. Any provisioning step failing removes the
     half-built `.venv` (mirrors the bash `trap 'rm -rf "$VENV"' ERR`,
     cleared only after a successful sentinel write) and returns exit 1.
  3. `exec` (replace-process, matching the oracle's `exec "$PY" -m pytest
     "$@"`) `.venv/bin/python -m pytest <argv>` — pytest's own exit code is
     the function's return value.

Port of: run-tests.sh (DoE 6fb5fb37, 2026-07-22)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Negative-spec:
  - Does NOT resolve the engine root, cc_invoke, or any coordinator-wide
    registry/env-config seam — `base_dir` is caller-resolved, exactly like
    `cruft_sweep.py`'s "handed fully-resolved paths" contract.
  - Does NOT reimplement the bash oracle's Windows-console-popup escape
    hatches (`popup-safe-env-suppressed` markers) — those exist because the
    oracle is a POSIX-only `#!/usr/bin/env bash` script that structurally
    never runs on Windows; this port keeps the same POSIX-only assumption
    (`.venv/bin/python`, not `.venv/Scripts/python.exe`) rather than silently
    broadening scope to a platform the oracle never covered.
  - Does NOT register as a JSON-RPC op (no `register_op`) — this is a
    template-variant #1 (direct-import) module per the R1 port template; the
    DoE trampoline imports `main` in-process, no IPC round-trip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from coordinator_core.ops._pytest_child_env import pytest_child_env
from coordinator_core.win_portability import no_console_passthrough_kwargs


def main(argv: list[str], base_dir: str | None = None) -> int:
    root = Path(base_dir) if base_dir is not None else Path.cwd()
    venv = root / ".venv"
    sentinel = venv / ".deps-installed"
    py = venv / "bin" / "python"

    if not sentinel.is_file():
        try:
            if not venv.is_dir():
                subprocess.run(
                    ["python3", "-m", "venv", str(venv)],
                    check=True,
                    cwd=str(root),
                    **no_console_passthrough_kwargs(),
                )
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                check=True,
                cwd=str(root),
                **no_console_passthrough_kwargs(),
            )
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "-e", "."],
                check=True,
                cwd=str(root),
                **no_console_passthrough_kwargs(),
            )
            subprocess.run(
                [str(py), "-m", "pip", "install", "--quiet", "pytest"],
                check=True,
                cwd=str(root),
                **no_console_passthrough_kwargs(),
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            shutil.rmtree(venv, ignore_errors=True)
            print(f"whoami-run-tests: venv provisioning failed: {exc}", file=sys.stderr)
            return 1
        sentinel.touch()

    result = subprocess.run(
        [str(py), "-m", "pytest", *argv],
        cwd=str(root),
        env=pytest_child_env(),
        **no_console_passthrough_kwargs(),
    )
    return result.returncode
