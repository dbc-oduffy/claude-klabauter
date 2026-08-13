"""
coordinator_core.ops.whoami_run_tests — venv-provisioning pytest launcher for
the coordinator_whoami standalone package.

Purpose: claude-klabauter-native port of the coordinator-claude-owned `coordinator/whoami/run-tests.sh`
CLI trampoline (DR-047 contract-vs-engine split). `coordinator_whoami` is a
standalone installable package (its own pyproject.toml, editable install,
jsonschema dependency) nested inside the coordinator-claude meta-repo — bare `pytest` under
the meta-repo's ambient interpreter cannot run its tests (missing editable
install, missing deps, possible stale-installed-copy shadowing). This module
owns the pure provisioning/launch logic; the coordinator-claude trampoline owns resolving
its own directory and passing it in.

Behavior mirrors the bash oracle line-for-line:
  1. Resolve `base_dir` (the coordinator-claude trampoline passes its own directory — the
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

Port of: run-tests.sh (coordinator-claude 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
  - Does NOT resolve CLAUDE_KLABAUTER_ROOT, cc_invoke, or any coordinator-wide
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
    coordinator-claude trampoline imports `main` in-process, no IPC round-trip.
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
    """Provision (idempotently) the coordinator_whoami .venv and run pytest.

    `base_dir` defaults to os.getcwd() for standalone testability; the coordinator-claude
    trampoline always passes its own resolved directory explicitly.

    Deliberate isolation boundary — do not convert to in-process calls.
    Mechanism: distinct interpreter + venv construction — `python3 -m venv`
    builds a dedicated venv, then every subsequent step runs under THAT
    venv's own python, never this process's `sys.executable`. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
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

    # env=pytest_child_env(): strip lazy-op registration before handing off to
    # pytest. The coordinator-claude trampoline imports `main` in-process, so this inherits
    # whatever that process carries; a pytest run that skips eager op
    # registration fails collection against every module asserting the op
    # registry at import time. Env hygiene for a spawned child, not the
    # resolver seam the negative-spec above declines to own.
    result = subprocess.run(
        [str(py), "-m", "pytest", *argv],
        cwd=str(root),
        env=pytest_child_env(),
        **no_console_passthrough_kwargs(),
    )
    return result.returncode
